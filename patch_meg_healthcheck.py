#!/usr/bin/env python3
"""
Patch MEG #2: aggiunge al fetcher
  (a) tracciamento errori reali per singola fonte (non solo silenziati)
  (b) due record di metadata scritti in TESTA a ogni JSONL generato:
      - record_type: "meg_protocol"  -> istruzioni di interpretazione/costruzione
        report, derivate da meg_protocol.yaml (fonte di verità v3.0)
      - record_type: "source_status" -> stato reale di ogni fonte configurata
        in questo run (ok / empty / error), con motivo dell'errore se presente

Idempotente: eseguibile più volte senza duplicare le modifiche.
Crea backup .bak2 prima di ogni scrittura (non sovrascrive il .bak
del patch INGV precedente).

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_healthcheck.py
"""

import re
import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak2")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")
    changed = False

    # ── 1. FETCH_ERRORS globale + helper, subito dopo USER_AGENTS ──
    marker1 = 'async def fetch_with_retry(client: httpx.AsyncClient, url: str,'
    if "FETCH_ERRORS" not in text:
        insert = '''# Registro errori per source-health-check — popolato dai fetch_* che
# falliscono realmente (non gli "empty legittimi", es. nessuno storm attivo).
# Azzerato a inizio di ogni fetch_all() per evitare leak tra run nello stesso processo.
FETCH_ERRORS: list[dict] = []

def record_error(source_id: str, area_id: str, msg: str):
    FETCH_ERRORS.append({
        "source_id": source_id,
        "area_id":   area_id,
        "error":     str(msg)[:300],
        "at":        now_utc(),
    })
    print(f"  [WARN] fonte fallita ({source_id}, area {area_id}): {msg}")


''' + marker1
        if marker1 in text:
            text = text.replace(marker1, insert, 1)
            changed = True
            print("  [OK] FETCH_ERRORS + record_error aggiunti")
        else:
            print("  [SKIP] anchor fetch_with_retry non trovato")
    else:
        print("  [SKIP] FETCH_ERRORS già presente")

    # ── 2. Rendi visibili gli errori nei fetch_* rimasti silenziosi ──
    targets = [
        ("fetch_usgs", r'(async def fetch_usgs\(.*?)except Exception:\n(\s*)return \[\]'),
        ("fetch_noaa_swpc", r'(async def fetch_noaa_swpc\(.*?)except Exception:\n(\s*)return \[\]'),
        ("fetch_nasa_eonet", r'(async def fetch_nasa_eonet\(.*?)except Exception:\n(\s*)return \[\]'),
    ]
    for fname, pattern in targets:
        def _sub(m, fname=fname):
            return (m.group(1) +
                    f'except Exception as e:\n{m.group(2)}'
                    f'record_error(feed.get("id","?"), area_id, e)\n{m.group(2)}return []')
        new_text, n = re.subn(pattern, _sub, text, count=1, flags=re.DOTALL)
        if n == 1:
            text = new_text
            changed = True
            print(f"  [OK] logging errori aggiunto a {fname}")
        elif f'record_error(feed.get("id"' in text and fname in text:
            print(f"  [SKIP] {fname} già patchato")
        else:
            print(f"  [ATTENZIONE] anchor {fname} non trovato — verificare a mano")

    # fetch_stooq_multi: except per-simbolo, struttura diversa (continue in loop)
    stooq_pattern = r'(async def fetch_stooq_multi\(.*?)except Exception:\n(\s*)continue(\s*# simbolo singolo non raggiungibile.*?\n)'
    def _sub_stooq(m):
        return (m.group(1) +
                f'except Exception as e:\n{m.group(2)}'
                f'record_error(f"{{feed.get(\'id\',\'?\')}}_{{symbol}}", area_id, e)\n{m.group(2)}continue' + m.group(3))
    new_text, n = re.subn(stooq_pattern, _sub_stooq, text, count=1, flags=re.DOTALL)
    if n == 1:
        text = new_text
        changed = True
        print("  [OK] logging errori aggiunto a fetch_stooq_multi (per-simbolo)")
    elif 'record_error(f"{feed.get(\'id\'' in text:
        print("  [SKIP] fetch_stooq_multi già patchato")
    else:
        print("  [ATTENZIONE] anchor fetch_stooq_multi non trovato — verificare a mano")

    # ── 3. Funzione che costruisce il record meta "meg_protocol" ──
    marker3 = "async def fetch_all(area_filter: Optional[str] = None,"
    if "def build_protocol_record(" not in text:
        insert3 = '''def build_protocol_record(protocol: dict) -> dict:
    """Record di istruzioni auto-contenuto: qualunque AI o umano che apre
    il JSONL trova qui come interpretare meg_flags, le soglie numeriche, e
    come costruire un report — senza dipendere da documenti esterni."""
    macro_areas_summary = {
        area_id: {
            "name":      area_def["name"],
            "mandatory": area_def.get("mandatory", False),
            "note":      (area_def.get("note") or "").strip() or None,
        }
        for area_id, area_def in protocol.get("macro_areas", {}).items()
    }
    return {
        "record_type": "meg_protocol",
        "protocol_version": protocol.get("protocol", {}).get("version"),
        "generated_at": now_utc(),
        "field_semantics": {
            "filled_by":            "'fetcher' = valore numerico strutturato, valutato deterministicamente. 'pending_reasoning' = testo libero, richiede lettura/interpretazione da parte di un reasoning engine.",
            "quality_flag":         protocol.get("quality_flags", {}),
            "alert_level":          {
                "INATTIVO":    "Nessuna soglia superata.",
                "WATCHLIST":   "Sotto soglia di monitoraggio ma in avvicinamento (>=70% della soglia).",
                "ATTIVO":      "Soglia di monitoraggio superata — evento rilevante confermato.",
                "ALLERTA_MAX": "Soglia di allerta massima superata — priorità assoluta nel report.",
            },
            "threshold_triggered":  "true se alert_level è ATTIVO o ALLERTA_MAX.",
            "tier":                 "1 = fonte istituzionale/primaria obbligatoria. 2 = fonte secondaria, ammessa con segnalazione esplicita.",
        },
        "report_construction_rules": [
            "Includere sempre: ogni evento con alert_level ATTIVO o ALLERTA_MAX, indipendentemente dall'area.",
            "Includere: eventi pending_reasoning il cui contenuto testuale suggerisce escalation, anche senza soglia numerica — vanno letti e valutati, non scartati automaticamente.",
            "Scartare/riassumere in una riga sola: eventi INATTIVO privi di rilevanza contestuale (es. sismicità di fondo sotto soglia, comunicati istituzionali di routine).",
            "Collegare causalmente eventi correlati tra aree diverse quando plausibile (es. B1 geopolitica -> B6 energia -> B7 mercati) invece di elencarli come fatti isolati.",
            "Se un record source_status riporta status 'error' per una fonte tier 1, questo VA segnalato esplicitamente in apertura di report — non va ignorato silenziosamente.",
            "Finestra di riferimento raccomandata: rolling 48h dal timestamp extracted_at più recente nel file, salvo diversa richiesta esplicita.",
            "In caso di dati contrastanti tra fonti, segnalare il conflitto esplicitamente invece di produrne una media o sintesi che lo nasconda.",
        ],
        "thresholds": protocol.get("thresholds", {}),
        "macro_areas": macro_areas_summary,
        "tier1_sources": protocol.get("tier1_sources", []),
    }


''' + marker3
        if marker3 in text:
            text = text.replace(marker3, insert3, 1)
            changed = True
            print("  [OK] build_protocol_record aggiunta")
        else:
            print("  [SKIP] anchor fetch_all non trovato per build_protocol_record")
    else:
        print("  [SKIP] build_protocol_record già presente")

    # ── 4. Integra i due meta-record nell'orchestratore fetch_all ──
    marker4 = '''    async with httpx.AsyncClient() as client:
        tasks = []
        meta  = []
        for area_id, area_def in target.items():
            label = area_def["label"]
            for feed in area_def["feeds"]:
                tasks.append(
                    fetch_one_feed(client, feed, area_id, label, protocol))
                meta.append((area_id, feed["id"]))
        results = await asyncio.gather(*tasks)'''
    marker4_new = '''    FETCH_ERRORS.clear()
    feed_lookup = {}  # (area_id, feed_id) -> feed dict (per source_status)

    async with httpx.AsyncClient() as client:
        tasks = []
        meta  = []
        for area_id, area_def in target.items():
            label = area_def["label"]
            for feed in area_def["feeds"]:
                tasks.append(
                    fetch_one_feed(client, feed, area_id, label, protocol))
                meta.append((area_id, feed["id"]))
                feed_lookup[(area_id, feed["id"])] = feed
        results = await asyncio.gather(*tasks)'''
    if marker4 in text:
        text = text.replace(marker4, marker4_new, 1)
        changed = True
        print("  [OK] fetch_all: tracking feed_lookup aggiunto")
    elif "feed_lookup = {}" in text:
        print("  [SKIP] feed_lookup già presente")
    else:
        print("  [ATTENZIONE] anchor blocco gather non trovato — verificare a mano")

    marker5 = '''    # Salva JSONL
    OUTPUT_DIR.mkdir(exist_ok=True)
    if output_path is None:
        output_path = OUTPUT_DIR / f"meg_events_{ts_file()}.jsonl"

    with open(output_path, "w", encoding="utf-8") as f:
        for ev in all_events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\\n")'''
    marker5_new = '''    # Costruisce source_status per ogni fonte interrogata in questo run
    error_by_source = {e["source_id"]: e for e in FETCH_ERRORS}
    source_status = []
    for (area_id, feed_id), events in zip(meta, results):
        feed = feed_lookup.get((area_id, feed_id), {})
        err = error_by_source.get(feed_id)
        if err:
            status = "error"
        elif len(events) == 0:
            status = "empty"
        else:
            status = "ok"
        source_status.append({
            "id":              feed_id,
            "area":            area_id,
            "label":           feed.get("label"),
            "tier":            feed.get("tier", 2),
            "type":            feed.get("type"),
            "status":          status,
            "events_returned": len(events),
            "error":           err["error"] if err else None,
        })

    # Salva JSONL — record di metadata sempre in testa, poi gli eventi
    OUTPUT_DIR.mkdir(exist_ok=True)
    if output_path is None:
        output_path = OUTPUT_DIR / f"meg_events_{ts_file()}.jsonl"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(build_protocol_record(protocol), ensure_ascii=False) + "\\n")
        f.write(json.dumps({
            "record_type": "source_status",
            "checked_at":  now_utc(),
            "sources":     source_status,
            "failed_count": sum(1 for s in source_status if s["status"] == "error"),
            "empty_count":  sum(1 for s in source_status if s["status"] == "empty"),
        }, ensure_ascii=False) + "\\n")
        for ev in all_events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\\n")'''
    if marker5 in text:
        text = text.replace(marker5, marker5_new, 1)
        changed = True
        print("  [OK] scrittura JSONL aggiornata con meta-record in testa")
    elif '"record_type": "source_status"' in text:
        print("  [SKIP] scrittura JSONL già patchata")
    else:
        print("  [ATTENZIONE] anchor blocco salvataggio JSONL non trovato — verificare a mano")

    if changed:
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("meg_fetcher.py aggiornato.")
    else:
        print("meg_fetcher.py: nessuna modifica necessaria (già patchato o anchor mancanti).")


if __name__ == "__main__":
    print("Patch meg_fetcher.py (source-health-check + protocol embedding)...")
    patch_fetcher()
    print()
    print("Verifica sintassi con:")
    print('  python3 -c "import ast; ast.parse(open(\'meg_fetcher.py\').read())"')
