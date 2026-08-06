#!/usr/bin/env python3
"""
Patch MEG #7: Copernicus EMS ha dismesso l'RSS a favore di un'API JSON
pubblica e strutturata (rapidmapping.emergency.copernicus.eu). Nuova
fonte con parser dedicato — dati più ricchi del vecchio RSS: paese,
categoria evento, tempo attivazione, stato (aperto/chiuso).

Idempotente. Backup .bak7.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_copernicus.py
"""

import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")
SOURCES = Path("meg_sources.yaml")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak7")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")
    changed = False

    marker1 = "async def fetch_rss_feed(client: httpx.AsyncClient, feed: dict,"
    if "async def fetch_copernicus_ems(" not in text:
        insert = '''async def fetch_copernicus_ems(client: httpx.AsyncClient, feed: dict,
                                area_id: str, area_label: str,
                                protocol: dict) -> list[dict]:
    """Fetch attivazioni Copernicus EMS dall'API JSON pubblica (l'RSS
    storico è stato dismesso). Dati più ricchi: paese, categoria,
    tempo di attivazione, stato aperto/chiuso."""
    events: list[dict] = []
    try:
        r = await client.get(feed["url"], timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        record_error(feed.get("id", "?"), area_id, e)
        return []

    for act in data.get("results", [])[:MAX_ITEMS]:
        code = act.get("code", "")
        name = act.get("name", "").strip()
        if not name:
            continue
        countries = ", ".join(act.get("countries", []))
        category = act.get("category", "")
        closed = act.get("closed", False)
        activation_time = act.get("activationTime", "")
        title = f"{code} — {name}"

        events.append({
            "event_id":       make_event_id(area_id, title, feed["id"]),
            "schema_version": SCHEMA_VER,
            "extracted_at":   now_utc(),
            "meg_area":       area_id,
            "meg_area_label": area_label,
            "source":         source_block(feed),
            "content": {
                "title":        title,
                "summary":      f"Categoria: {category} | Paesi: {countries} | Stato: {'chiusa' if closed else 'attiva'}",
                "original_url": f"https://mapping.emergency.copernicus.eu/activations/{code}/",
                "published_at": activation_time or None,
                "language":     "en",
            },
            "meg_flags": pending_flags("Attivazione Copernicus EMS — impatto da valutare"),
        })
    return events


''' + marker1
        if marker1 in text:
            text = text.replace(marker1, insert, 1)
            changed = True
            print("  [OK] fetch_copernicus_ems aggiunta")
        else:
            print("  [SKIP] anchor fetch_rss_feed non trovato")
    else:
        print("  [SKIP] fetch_copernicus_ems già presente")

    marker2 = '    elif ftype == "alphavantage_multi":\n        return await fetch_alphavantage_multi(client, feed, area_id, area_label, protocol)\n'
    marker2_new = marker2 + '    elif ftype == "copernicus_ems_json":\n        return await fetch_copernicus_ems(client, feed, area_id, area_label, protocol)\n'
    if 'ftype == "copernicus_ems_json"' not in text:
        if marker2 in text:
            text = text.replace(marker2, marker2_new, 1)
            changed = True
            print("  [OK] dispatcher aggiornato con ramo copernicus_ems_json")
        else:
            print("  [ATTENZIONE] anchor dispatcher alphavantage_multi non trovato")
    else:
        print("  [SKIP] ramo copernicus_ems_json già registrato")

    if changed:
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("meg_fetcher.py aggiornato.")
    else:
        print("meg_fetcher.py: nessuna modifica necessaria.")


def patch_sources():
    text = SOURCES.read_text(encoding="utf-8")
    if "copernicus_ems_json" in text:
        print("meg_sources.yaml: fonte già aggiornata, nessuna modifica.")
        return

    old = '''    - id: copernicus_ems
      label: Copernicus EMS
      url: https://emergency.copernicus.eu/mapping/list-of-activations-rapid/rss
      type: rss
      tier: 1'''
    new = '''    - id: copernicus_ems
      label: Copernicus EMS (API JSON)
      url: "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/?limit=15"
      type: copernicus_ems_json
      tier: 1'''
    if old in text:
        text = text.replace(old, new, 1)
        backup(SOURCES)
        SOURCES.write_text(text, encoding="utf-8")
        print("meg_sources.yaml aggiornato: copernicus_ems su API JSON")
    else:
        print("  [ATTENZIONE] blocco copernicus_ems non trovato esattamente come atteso.")


if __name__ == "__main__":
    print("Patch Copernicus EMS (API JSON al posto dell'RSS dismesso)...")
    patch_fetcher()
    patch_sources()
    print()
    print("Verifica sintassi con:")
    print('  python3 -c "import ast; ast.parse(open(\'meg_fetcher.py\').read())"')
