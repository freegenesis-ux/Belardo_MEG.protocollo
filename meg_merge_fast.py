#!/usr/bin/env python3
"""
MEG - fusione degli aggiornamenti "fast" (ogni 15 min) dentro latest.jsonl.

Il link pubblico serve un solo file, latest.jsonl. Il ciclo "full" (ogni ora) lo
rigenera intero; il ciclo "fast" aggiorna solo alcune fonti (mercati, sismologia).
Questo script prende l'ultimo latest.jsonl e ci sostituisce, FONTE PER FONTE,
cio' che il ciclo fast ha appena letto, lasciando intatto tutto il resto.

Regole (conservative, in linea con le report_construction_rules del protocollo):
  - gli eventi di una fonte si sostituiscono solo se la fonte fast ha status "ok" E il
    file fast contiene davvero eventi di quella fonte; altrimenti restano i precedenti;
  - le fonti "generaliste" smistate da AI (area "generalist...") NON si toccano: i loro
    eventi sono distribuiti su piu' aree e un ciclo fast ne vede solo una parte; le
    aggiorna solo il ciclo full;
  - gli eventi di una fonte possono avere id con suffisso (es. yahoo_finance_multi_^GSPC):
    l'abbinamento e' per id esatto o prefisso "<id>_";
  - la riga della fonte in source_status si aggiorna sempre, tranne "throttled"
    (fonte volutamente non interrogata: resta lo stato del ciclo precedente);
    quindi un guasto fast e' visibile, non nascosto;
  - data_quality = il PEGGIORE tra latest.jsonl e i file fast (mai migliorato
    artificialmente); si riallinea da solo al prossimo ciclo full;
  - nell'intestazione meg_protocol compare il blocco "refresh" con last_full_at e
    last_fast_at, cosi' chi legge sa quanto e' fresca ogni parte del file.

Solo libreria standard. Uso:
    python3 meg_merge_fast.py --base meg_events/latest.jsonl \
        --fast meg_events/latest_fast_b7.jsonl --fast meg_events/latest_fast_b2.jsonl \
        --out meg_events/latest.jsonl
"""
import argparse
import json
import sys

LEVELS = ["COMPLETE", "DEGRADED", "PARTIAL", "CORRUPT_SUSPECT"]


def load(path):
    head = status = None
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            rt = o.get("record_type")
            if rt == "meg_protocol":
                head = o
            elif rt == "source_status":
                status = o
            elif rt is None:
                events.append(o)
    if head is None or status is None:
        raise ValueError(f"{path}: manca meg_protocol o source_status")
    return head, status, events


def belongs(event_source_id, status_id):
    return event_source_id == status_id or (event_source_id or "").startswith(status_id + "_")


def worst(levels):
    idx = [LEVELS.index(l) for l in levels if l in LEVELS]
    return LEVELS[max(idx)] if idx else "COMPLETE"


def merge(base_path, fast_paths):
    head, status, events = load(base_path)
    sources = {s["id"]: s for s in status["sources"]}
    order = [s["id"] for s in status["sources"]]

    replace_events = set()      # fonti di cui sostituire gli eventi
    new_events = []
    fast_levels, reasons, fast_ids = [], list(status.get("data_quality_reasons") or []), []
    last_fast = None

    for p in fast_paths:
        fh, fs, fe = load(p)
        last_fast = max(last_fast or fh["generated_at"], fh["generated_at"])
        fast_levels.append(fs.get("data_quality", "COMPLETE"))
        for r in fs.get("data_quality_reasons") or []:
            if r not in reasons:
                reasons.append(r)
        fast_events_here = fe
        for s in fs["sources"]:
            if s["status"] == "throttled":
                continue
            if str(s.get("area", "")).startswith("generalist"):
                continue  # solo il ciclo full
            if s["id"] not in sources:
                order.append(s["id"])
            sources[s["id"]] = s
            fast_ids.append(s["id"])
            if s["status"] == "ok" and any(belongs(e.get("source", {}).get("id"), s["id"]) for e in fast_events_here):
                replace_events.add(s["id"])
        new_events.extend(fe)

    def is_replaced(e):
        sid = e.get("source", {}).get("id")
        return any(belongs(sid, r) for r in replace_events)

    new_events = [e for e in new_events if is_replaced(e)]
    kept = [e for e in events if not is_replaced(e)]
    seen = {e.get("event_id") for e in kept}
    added = [e for e in new_events if e.get("event_id") not in seen]
    merged_events = kept + added

    merged_sources = [sources[i] for i in order]
    failed = [s["id"] for s in merged_sources if s["status"] in ("error", "ai_unavailable")]
    degraded = [s["id"] for s in merged_sources if s["status"] == "degraded_fallback"]

    prev_refresh = head.get("refresh") or {}
    last_full = prev_refresh.get("last_full_at") or head["generated_at"]
    head = dict(head)
    head["generated_at"] = last_fast or head["generated_at"]
    head["refresh"] = {
        "last_full_at": last_full,
        "last_fast_at": last_fast,
        "fast_sources_refreshed": sorted(set(fast_ids)),
        "note": "latest.jsonl: ciclo full ogni ora; ogni 15 min le fonti elencate in "
                "fast_sources_refreshed sono rilette e fuse in questo file.",
    }

    status = dict(status)
    status["checked_at"] = last_fast or status["checked_at"]
    status["sources"] = merged_sources
    status["failed_count"] = len(failed)
    status["degraded_count"] = len(degraded)
    status["empty_count"] = sum(1 for s in merged_sources if s["status"] == "empty")
    status["failed_sources"] = failed if failed else status.get("failed_sources", [])
    status["degraded_sources"] = degraded if degraded else status.get("degraded_sources", [])
    status["data_quality"] = worst([status.get("data_quality", "COMPLETE")] + fast_levels)
    status["data_quality_reasons"] = reasons
    status["events_previous_run"] = status.get("events_total", len(events))
    status["events_total"] = len(merged_events)
    return head, status, merged_events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--fast", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    head, status, events = merge(a.base, a.fast)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(head, ensure_ascii=False) + "\n")
        f.write(json.dumps(status, ensure_ascii=False) + "\n")
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    import os
    os.replace(tmp, a.out)
    print(f"MEG merge: {len(events)} eventi, generated_at={head['generated_at']}, "
          f"data_quality={status['data_quality']}, fonti fast fuse={len(head['refresh']['fast_sources_refreshed'])}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # nessun file parziale: il workflow lascia latest.jsonl com'era
        print(f"MEG merge FALLITO: {e}", file=sys.stderr)
        sys.exit(1)
