#!/usr/bin/env python3
"""
Patch MEG 7: controllo di integrita' dei dati.
 A. meg_fetcher.py
    - assess_data_quality(): COMPLETE / DEGRADED / PARTIAL / CORRUPT_SUSPECT + motivi
    - il record source_status ora contiene: data_quality, data_quality_reasons, blind_areas,
      failed_sources, degraded_sources, events_total, events_previous_run
    - stato 'throttled' per alphavantage_multi quando non interrogata di proposito
    - il record meg_protocol spiega agli altri AI cosa fare quando data_quality != COMPLETE
 B. workflow: step "Validazione integrita'" (usa meg_validate.py): se il file e' corrotto
    NON viene pubblicato (si ripristina l'ultima versione valida) e il run fallisce in modo visibile
 C. Opzionale  --with-fast-gating : i run fast (--area) interrogano solo le fonti generalist con
    'fast: true' (gdacs_ai, marketwatch_ai), invece di tutte (riduce chiamate AI e carico sulle fonti)
Idempotente, tutto-o-niente, con backup.
"""
import ast, shutil, sys
from pathlib import Path

GATING = "--with-fast-gating" in sys.argv
F = Path("meg_fetcher.py")
W = Path(".github/workflows/meg_fetch.yml")
Y = Path("meg_sources.yaml")
src = F.read_text(encoding="utf-8")
if "assess_data_quality" in src:
    print("Gia' applicata (assess_data_quality presente). Nessuna modifica."); sys.exit(0)
if not Path("meg_validate.py").exists():
    print("Manca meg_validate.py nella cartella del repo: copialo qui prima."); sys.exit(1)

HELPERS = r'''def _count_events_in_file(path):
    """Eventi (righe senza record_type) di un JSONL esistente; None se illeggibile."""
    try:
        n = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict) and "record_type" not in o:
                    n += 1
        return n
    except Exception:
        return None


def assess_data_quality(source_status, stats, protocol, area_filter, prev_total):
    """Valuta quanto e' affidabile il file prodotto. Livelli (dal migliore al peggiore):
    COMPLETE, DEGRADED, PARTIAL, CORRUPT_SUSPECT. Ritorna un dict da inserire in source_status."""
    order = ["COMPLETE", "DEGRADED", "PARTIAL", "CORRUPT_SUSPECT"]
    level = "COMPLETE"
    reasons = []

    def bump(new):
        nonlocal level
        if order.index(new) > order.index(level):
            level = new

    n_src = max(len(source_status), 1)
    failed = [s for s in source_status if s["status"] in ("error", "ai_unavailable")]
    degraded = [s for s in source_status if s["status"] == "degraded_fallback"]
    total = stats.get("total", 0)

    if total == 0:
        bump("CORRUPT_SUSPECT")
        reasons.append("0 eventi raccolti")
    if prev_total and prev_total >= 20 and total < 0.5 * prev_total:
        bump("CORRUPT_SUSPECT")
        reasons.append(f"eventi crollati: {total} contro {prev_total} del run precedente")
    if len(failed) / n_src >= 0.4:
        bump("CORRUPT_SUSPECT")
        reasons.append(f"oltre il 40% delle fonti non risponde ({len(failed)}/{n_src})")
    elif len(failed) / n_src >= 0.15:
        bump("PARTIAL")
        reasons.append(f"{len(failed)}/{n_src} fonti non rispondono")

    t1 = [s["id"] for s in failed if s.get("tier", 2) == 1]
    t2 = [s["id"] for s in failed if s.get("tier", 2) != 1]
    if t1:
        bump("PARTIAL")
        reasons.append("fonti tier 1 non disponibili: " + ", ".join(t1))
    if t2:
        bump("DEGRADED")
        reasons.append("fonti tier 2 non disponibili: " + ", ".join(t2))
    if degraded:
        bump("DEGRADED")
        reasons.append("notizie senza smistamento AI completo in: " + ", ".join(s["id"] for s in degraded))

    areas = [area_filter] if area_filter else list(protocol.get("macro_areas", {}).keys())
    blind = [a for a in areas if stats.get("by_area", {}).get(a, 0) == 0]
    failed_areas = {s.get("area") for s in failed}
    blind_hard = [a for a in blind if a in failed_areas]
    blind_quiet = [a for a in blind if a not in failed_areas]
    if blind_hard:
        bump("PARTIAL")
        reasons.append("aree CIECHE (senza eventi e con fonti guaste): " + ", ".join(blind_hard))
    if blind_quiet:
        bump("DEGRADED")
        reasons.append("aree senza eventi in questo run (nessuna fonte guasta): " + ", ".join(blind_quiet))

    return {
        "data_quality": level,
        "data_quality_reasons": reasons,
        "blind_areas": blind,
        "failed_sources": [s["id"] for s in failed],
        "degraded_sources": [s["id"] for s in degraded],
        "events_total": total,
        "events_previous_run": prev_total,
    }


def build_protocol_record(protocol: dict) -> dict:'''

PROTO_ADD = r'''            "Il record source_status contiene data_quality (COMPLETE | DEGRADED | PARTIAL | CORRUPT_SUSPECT), data_quality_reasons, blind_areas, failed_sources, degraded_sources. Leggerlo SEMPRE prima di scrivere il report.",
            "Se data_quality e' diverso da COMPLETE il report DEVE aprire con una riga 'QUALITA' DATI: <livello>' seguita dai motivi, dalle fonti non disponibili e dalle aree cieche.",
            "Per le aree cieche e per le fonti non disponibili NON scrivere 'nessun evento' o 'nessuna novita': scrivere 'non osservabile (fonte non disponibile)'. L'assenza di dati non e' assenza di eventi.",
            "Con DEGRADED o PARTIAL abbassare esplicitamente la confidenza di ogni incrocio causale che coinvolge un'area cieca o una fonte non disponibile, e dirlo.",
            "Con CORRUPT_SUSPECT NON produrre previsioni ne' incroci causali: dichiarare che il file non e' affidabile e invitare a riscaricarlo dopo il prossimo ciclo di raccolta.",
            "Gli eventi con quality_flag FALLBACK_NO_AI hanno l'area assegnata da una regola e non dall'AI: trattarli come non verificati.",
        ],'''

REPL_FETCHER = [
    # helper prima di build_protocol_record
    (r'''def build_protocol_record(protocol: dict) -> dict:''', HELPERS),
    # semantica quality_flag + stati
    (r'''            "quality_flag":         protocol.get("quality_flags", {}),''',
     r'''            "quality_flag":         {**protocol.get("quality_flags", {}),
                                     "CLASSIFICATO_AI": "Area assegnata da un modello AI di smistamento (feed generalista).",
                                     "FALLBACK_NO_AI": "Area assegnata da regola (area fissa del feed o parole chiave) perche' l'AI non era disponibile: non verificato."},
            "source_status_values": {
                "ok": "Fonte interrogata, eventi restituiti.",
                "empty": "Fonte valida ma senza notizie nel periodo (non e' un guasto).",
                "throttled": "Fonte volutamente non interrogata in questo run per rispettare i limiti del servizio (non e' un guasto).",
                "error": "Fonte guasta o bloccata: i suoi eventi mancano.",
                "ai_unavailable": "Smistamento AI non disponibile e nessun fallback: le notizie di questa fonte sono perse.",
                "degraded_fallback": "Alcune notizie senza smistamento AI completo (area da regola o scartate).",
            },'''),
    # regole del report
    (r'''            "In caso di dati contrastanti tra fonti, segnalare il conflitto esplicitamente invece di produrne una media o sintesi che lo nasconda.",
        ],''',
     r'''            "In caso di dati contrastanti tra fonti, segnalare il conflitto esplicitamente invece di produrne una media o sintesi che lo nasconda.",
''' + PROTO_ADD),
    # throttled
    (r'''        elif len(events) == 0:
            status = "empty"''',
     r'''        elif len(events) == 0 and feed.get("type") == "alphavantage_multi":
            status = "throttled"
        elif len(events) == 0:
            status = "empty"'''),
    # calcolo qualita' prima della scrittura
    (r'''    if output_path is None:
        output_path = OUTPUT_DIR / f"meg_events_{ts_file()}.jsonl"
''',
     r'''    if output_path is None:
        output_path = OUTPUT_DIR / f"meg_events_{ts_file()}.jsonl"
    prev_total = _count_events_in_file(output_path) if Path(output_path).exists() else None
    dq = assess_data_quality(source_status, stats, protocol, area_filter, prev_total)
'''),
    (r'''            "empty_count":  sum(1 for s in source_status if s["status"] == "empty"),
        }, ensure_ascii=False) + "\n")''',
     r'''            "empty_count":  sum(1 for s in source_status if s["status"] == "empty"),
            **dq,
        }, ensure_ascii=False) + "\n")'''),
]
if GATING:
    REPL_FETCHER.append((
        r'''    generalist_sources = load_generalist_sources()
''',
        r'''    generalist_sources = [f for f in load_generalist_sources()
                          if not area_filter or f.get("fast")]
'''))

new = src
for old, rep in REPL_FETCHER:
    n = new.count(old)
    if n != 1:
        print(f"ATTENZIONE: ancora trovata {n} volte (attesa 1), nessuna modifica scritta:\n---\n{old[:150]}\n---")
        sys.exit(1)
    new = new.replace(old, rep, 1)
try:
    ast.parse(new)
except SyntaxError as e:
    print("ERRORE sintassi fetcher, nessuna modifica scritta:", e); sys.exit(1)

# ---- workflow ---------------------------------------------------------------
VALIDATE_STEP = r'''      - name: Validazione integrità (se corrotto non si pubblica e il run fallisce)
        id: validate
        run: |
          set +e
          corrupt=0
          check() {
            f="$1"
            [ -f "$f" ] || return 0
            git show "HEAD:$f" > /tmp/prev.jsonl 2>/dev/null || : > /tmp/prev.jsonl
            python3 meg_validate.py "$f" --previous /tmp/prev.jsonl
            if [ $? -ne 0 ]; then
              echo "::error::$f corrotto o gravemente parziale: ripristino l'ultima versione valida"
              git checkout -- "$f" 2>/dev/null
              corrupt=1
            fi
          }
          if [ "${{ steps.mode.outputs.value }}" = "full" ]; then
            check meg_events/latest.jsonl
          else
            check meg_events/latest_fast_b7.jsonl
            check meg_events/latest_fast_b2.jsonl
            cat meg_events/latest_fast_b7.jsonl meg_events/latest_fast_b2.jsonl > meg_events/latest_fast.jsonl
          fi
          echo "corrupt=$corrupt" >> "$GITHUB_OUTPUT"

'''
FINAL_STEP = r'''
      - name: Segnala fallimento se i dati erano corrotti
        if: steps.validate.outputs.corrupt == '1'
        run: |
          echo "::error::Dati corrotti o gravemente parziali: e' rimasta pubblicata l'ultima versione valida. Vedi lo step Validazione integrità."
          exit 1
'''
wnew = None
if W.exists():
    wsrc = W.read_text(encoding="utf-8")
    if "id: validate" not in wsrc:
        a1 = "      - name: Copia in archivio timestampato (solo run full)\n        if: steps.mode.outputs.value == 'full'\n"
        if wsrc.count(a1) != 1:
            print("ATTENZIONE: step 'Copia in archivio' non trovato nel workflow, nessuna modifica scritta"); sys.exit(1)
        wnew = wsrc.replace(a1, VALIDATE_STEP + "      - name: Copia in archivio timestampato (solo run full)\n        if: steps.mode.outputs.value == 'full' && steps.validate.outputs.corrupt != '1'\n", 1)
        if not wnew.endswith("\n"):
            wnew += "\n"
        wnew += FINAL_STEP
        try:
            import yaml
            d = yaml.safe_load(wnew)
            names = [s.get("name") for s in d["jobs"]["fetch"]["steps"] if isinstance(s, dict)]
            assert "Segnala fallimento se i dati erano corrotti" in names
        except Exception as e:
            print("ERRORE validazione workflow, nessuna modifica scritta:", e); sys.exit(1)
else:
    print("ATTENZIONE: workflow non trovato, salto la parte workflow")

# ---- yaml fast gating -------------------------------------------------------
ynew = None
if GATING and Y.exists():
    ysrc = Y.read_text(encoding="utf-8")
    ynew = ysrc
    for fid in ("gdacs_ai", "marketwatch_ai"):
        anc = f"- id: {fid}\n"
        if ynew.count(anc) == 1 and f"- id: {fid}\n  fast: true\n" not in ynew:
            ynew = ynew.replace(anc, anc + "  fast: true\n", 1)
        elif ynew.count(anc) != 1:
            print(f"ATTENZIONE: {fid} non trovata una sola volta nel YAML, nessuna modifica scritta"); sys.exit(1)

shutil.copy(F, "meg_fetcher.py.bak_integrity")
F.write_text(new, encoding="utf-8")
msgs = ["fetcher: data_quality + regole nel protocollo + stato throttled"]
if wnew:
    shutil.copy(W, "meg_fetch.yml.bak_integrity")
    W.write_text(wnew, encoding="utf-8")
    msgs.append("workflow: validazione integrita' + fallimento visibile")
if ynew and ynew != ysrc:
    shutil.copy(Y, "meg_sources.yaml.bak_integrity")
    Y.write_text(ynew, encoding="utf-8")
    msgs.append("fast gating attivo (gdacs_ai, marketwatch_ai: fast: true)")
print("OK: " + "; ".join(msgs))
