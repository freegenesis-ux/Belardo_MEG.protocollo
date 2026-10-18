#!/usr/bin/env python3
"""
MEG - validatore di integrita' di un file JSONL prodotto dal fetcher.
Solo libreria standard. Uso:

    python3 meg_validate.py FILE [--previous FILE_PRECEDENTE] [--min-ratio 0.5]

Codici di uscita:
    0  file utilizzabile (puo' comunque essere DEGRADED o PARTIAL: vengono stampati avvisi)
    2  file CORROTTO o gravemente parziale: NON va pubblicato / NON va usato per un report

Controlli indipendenti dal fetcher (ricalcolati qui, non letti dal file):
    - ogni riga e' JSON valido (riga troncata = file corrotto)
    - presenti i record di testa meg_protocol e source_status
    - almeno un evento, con event_id / meg_area / content.title
    - numero di eventi non crollato rispetto al file precedente
E controllo del campo data_quality dichiarato dal fetcher nel record source_status.
"""
import argparse
import json
import sys


def load(path):
    heads, events, bad = [], [], []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception as e:
                bad.append((n, str(e)[:70]))
                continue
            if not isinstance(o, dict):
                bad.append((n, "riga non e' un oggetto JSON"))
                continue
            (heads if "record_type" in o else events).append(o)
    return heads, events, bad


def count_events(path):
    try:
        return len(load(path)[1])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--previous")
    ap.add_argument("--min-ratio", type=float, default=0.5)
    a = ap.parse_args()

    corrupt, warn = [], []
    try:
        heads, events, bad = load(a.file)
    except Exception as e:
        print(f"::error::{a.file}: impossibile leggere il file ({e})")
        return 2

    if bad:
        corrupt.append(f"{len(bad)} righe non valide (prima: riga {bad[0][0]}: {bad[0][1]}) - file troncato o corrotto")
    types = {h.get("record_type") for h in heads}
    for need in ("meg_protocol", "source_status"):
        if need not in types:
            corrupt.append(f"record di testa '{need}' mancante")
    if not events:
        corrupt.append("nessun evento nel file")

    if events:
        incomplete = [e for e in events
                      if not (e.get("event_id") and e.get("meg_area") and (e.get("content") or {}).get("title"))]
        if len(incomplete) > 0.05 * len(events):
            corrupt.append(f"{len(incomplete)}/{len(events)} eventi senza event_id/meg_area/titolo")
        ids = [e.get("event_id") for e in events]
        dup = len(ids) - len(set(ids))
        if dup:
            warn.append(f"{dup} event_id duplicati")

    if a.previous:
        prev = count_events(a.previous)
        if prev and prev >= 20 and len(events) < a.min_ratio * prev:
            corrupt.append(f"eventi crollati: {len(events)} contro {prev} del file precedente "
                           f"(soglia {int(a.min_ratio * 100)}%)")

    declared = [h for h in heads if h.get("record_type") == "source_status"]
    for h in declared:
        dq = h.get("data_quality")
        reasons = "; ".join(h.get("data_quality_reasons") or [])
        if dq is None:
            warn.append("campo data_quality assente nel source_status")
        elif dq == "CORRUPT_SUSPECT":
            corrupt.append(f"il fetcher dichiara CORRUPT_SUSPECT: {reasons}")
        elif dq in ("PARTIAL", "DEGRADED"):
            warn.append(f"data_quality={dq}: {reasons}")

    print(f"{a.file}: {len(events)} eventi, {len(heads)} record di testa, "
          f"{len(bad)} righe non valide")
    for w in warn:
        print(f"::warning::{a.file}: {w}")
    if corrupt:
        for c in corrupt:
            print(f"::error::{a.file}: {c}")
        return 2
    print("VALIDAZIONE: file utilizzabile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
