#!/usr/bin/env python3
"""
Patch MEG: aggiunge la fonte sismica INGV (FDSNWS event webservice)
in sostituzione della vecchia RSS morta (cnt.rm.ingv.it), con soglia
di allerta locale abbassata per aree vulcaniche attive (Campi Flegrei,
Vesuvio) e visibilità degli errori di fetch nei log.

Idempotente: eseguibile più volte senza duplicare le modifiche.
Crea backup .bak prima di ogni scrittura.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_ingv.py
"""

import re
import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")
SOURCES = Path("meg_sources.yaml")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")
    changed = False

    # ── 1. Inserisci normalize_ingv_event prima di normalize_noaa_alert ──
    marker_norm = "def normalize_noaa_alert(alert: dict, feed: dict,"
    if "def normalize_ingv_event(" not in text:
        new_func = '''def normalize_ingv_event(fields: list, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> Optional[dict]:
    """Parser per il formato pipe-delimited del webservice FDSNWS-event
    INGV: EventID|Time|Lat|Lon|Depth|Author|Catalog|Contributor|
    ContributorID|MagType|Magnitude|MagAuthor|EventLocationName|EventType"""
    if len(fields) < 13:
        return None
    try:
        event_id_raw = fields[0].strip()
        time_str     = fields[1].strip()
        depth        = fields[4].strip()
        mag_str      = fields[10].strip()
        location     = fields[12].strip()
    except IndexError:
        return None

    if not mag_str:
        return None
    try:
        mag = float(mag_str)
    except ValueError:
        return None

    # Normalizza timestamp: "2026-07-31T17:46:43.740000" -> "...Z" (troncato ai secondi)
    t_str = time_str.split(".")[0] + "Z" if time_str else None
    title = f"INGV M{mag} — {location}" if location else f"INGV M{mag}"
    depth_note = f"Profondità: {depth} km" if depth else ""

    triggered, level = evaluate_threshold(mag, "earthquake_magnitude", protocol)

    # Soglia locale abbassata per aree vulcaniche attive (bradisismo Campi
    # Flegrei / Vesuvio): un M3.0 lì è operativamente rilevante anche se
    # sotto la soglia globale pensata per sismicità generica mondiale.
    loc_lower = location.lower()
    is_volcanic_area = "flegrei" in loc_lower or "vesuv" in loc_lower or "pozzuoli" in loc_lower
    note_suffix = ""
    if is_volcanic_area and mag >= 3.0 and level in ("INATTIVO", "WATCHLIST"):
        triggered, level = True, "ATTIVO"
        note_suffix = " [soglia locale area vulcanica attiva — bradisismo]"

    return {
        "event_id":       make_event_id(area_id, title, feed["id"]),
        "schema_version": SCHEMA_VER,
        "extracted_at":   now_utc(),
        "meg_area":       area_id,
        "meg_area_label": area_label,
        "source":         source_block(feed),
        "content": {
            "title":        title,
            "summary":      f"Magnitudo {mag} | {location} | {depth_note}".strip(" |"),
            "original_url": f"https://terremoti.ingv.it/event/{event_id_raw}" if event_id_raw else None,
            "published_at": t_str,
            "language":     "it",
        },
        "meg_flags": fetcher_flags(
            threshold_key="earthquake_magnitude",
            value=mag,
            unit="Md",
            triggered=triggered,
            level=level,
            notes=(depth_note + note_suffix).strip(),
        ),
    }


''' + marker_norm
        if marker_norm in text:
            text = text.replace(marker_norm, new_func, 1)
            changed = True
            print("  [OK] normalize_ingv_event aggiunta")
        else:
            print("  [SKIP] anchor normalize_noaa_alert non trovato — inserimento norm saltato")
    else:
        print("  [SKIP] normalize_ingv_event già presente")

    # ── 2. Inserisci fetch_ingv_events prima di fetch_one_feed ──
    marker_fetch = "async def fetch_one_feed(client: httpx.AsyncClient, feed: dict,"
    if "async def fetch_ingv_events(" not in text:
        new_fetch = '''async def fetch_ingv_events(client: httpx.AsyncClient, feed: dict,
                             area_id: str, area_label: str,
                             protocol: dict) -> list[dict]:
    """Fetch dal webservice FDSNWS-event INGV — formato testo pipe-delimited,
    finestra temporale dinamica (ultime 24h), soglia minmag configurabile
    nella fonte (default 1.5)."""
    start = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)) \\
        .strftime("%Y-%m-%dT%H:%M:%S")
    minmag = feed.get("minmag", 1.5)
    url = (f"{feed['url']}?starttime={start}&minmag={minmag}"
           f"&format=text&orderby=time")
    events: list[dict] = []
    try:
        r = await fetch_with_retry(client, url, timeout=FETCH_TIMEOUT, attempts=2)
        lines = [ln for ln in r.text.splitlines() if ln.strip() and not ln.startswith("#")]
        for line in lines[:MAX_ITEMS * 3]:  # finestra INGV più densa di USGS
            fields = line.split("|")
            ev = normalize_ingv_event(fields, feed, area_id, area_label, protocol)
            if ev:
                events.append(ev)
    except Exception as e:
        print(f"  [WARN] fetch_ingv_events fallito ({feed.get('id')}): {e}")
    return events


''' + marker_fetch
        if marker_fetch in text:
            text = text.replace(marker_fetch, new_fetch, 1)
            changed = True
            print("  [OK] fetch_ingv_events aggiunta")
        else:
            print("  [SKIP] anchor fetch_one_feed non trovato — inserimento fetch saltato")
    else:
        print("  [SKIP] fetch_ingv_events già presente")

    # ── 3. Registra il nuovo type nel dispatcher ──
    dispatch_anchor = '    elif ftype == "geojson":\n        return await fetch_usgs(client, feed, area_id, area_label, protocol)\n'
    dispatch_new = dispatch_anchor + '    elif ftype == "ingv_text":\n        return await fetch_ingv_events(client, feed, area_id, area_label, protocol)\n'
    if 'ftype == "ingv_text"' not in text:
        if dispatch_anchor in text:
            text = text.replace(dispatch_anchor, dispatch_new, 1)
            changed = True
            print("  [OK] dispatcher aggiornato con ramo ingv_text")
        else:
            print("  [SKIP] anchor dispatcher geojson non trovato")
    else:
        print("  [SKIP] ramo ingv_text già registrato nel dispatcher")

    # ── 4. Visibilità errori: logga le eccezioni silenziate nei fetcher esistenti ──
    silent_patterns = [
        ('    except Exception as e:\n        return []  # Silenzioso — il server logga altrove',
         '    except Exception as e:\n        print(f"  [WARN] fetch_rss_feed fallito ({feed.get(\'id\')}): {e}")\n        return []'),
    ]
    for old, new in silent_patterns:
        if old in text:
            text = text.replace(old, new, 1)
            changed = True
            print("  [OK] logging errori aggiunto a fetch_rss_feed")

    if changed:
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("meg_fetcher.py aggiornato.")
    else:
        print("meg_fetcher.py: nessuna modifica necessaria (già patchato o anchor mancanti).")


def patch_sources():
    text = SOURCES.read_text(encoding="utf-8")
    old_block = (
        "    - id: ingv_rss\n"
        "      label: INGV Sismico\n"
        "      url: http://cnt.rm.ingv.it/feed/atom/2.5/week\n"
        "      type: rss\n"
        "      tier: 1\n"
    )
    new_block = (
        "    - id: ingv_webservice\n"
        "      label: INGV Eventi Sismici (FDSNWS)\n"
        "      url: https://webservices.ingv.it/fdsnws/event/1/query\n"
        "      type: ingv_text\n"
        "      tier: 1\n"
        "      minmag: 1.5\n"
    )
    if "ingv_webservice" in text:
        print("meg_sources.yaml: fonte già aggiornata, nessuna modifica.")
        return
    if old_block in text:
        text = text.replace(old_block, new_block, 1)
        backup(SOURCES)
        SOURCES.write_text(text, encoding="utf-8")
        print("meg_sources.yaml aggiornato: ingv_rss -> ingv_webservice")
    else:
        print("  [ATTENZIONE] blocco ingv_rss non trovato esattamente come atteso.")
        print("  Verifica manualmente meg_sources.yaml — la fonte INGV va sostituita a mano.")


if __name__ == "__main__":
    print("Patch meg_fetcher.py...")
    patch_fetcher()
    print()
    print("Patch meg_sources.yaml...")
    patch_sources()
    print()
    print("Fatto. Verifica con: python3 -c \"import ast; ast.parse(open('meg_fetcher.py').read())\" (controllo sintassi)")
