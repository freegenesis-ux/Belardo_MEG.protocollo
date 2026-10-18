#!/usr/bin/env python3
"""
Patch MEG 3 (dopo patch_meg_groq_cache2.py): catena di modelli Groq con quote separate.
 - llama-3.1-8b-instant rispondeva 404 sull'account: rimosso come default
 - catena: gpt-oss-20b -> gpt-oss-120b -> llama-3.3-70b-versatile -> qwen/qwen3.8-27b
   (sostituibile con GROQ_FALLBACK_MODELS="a,b,c")
 - su 404/400 (modello inesistente o parametro rifiutato) passa al modello successivo
   e stampa il corpo dell'errore nel log
Idempotente, tutto-o-niente, con backup.
"""
import ast, shutil, sys
from pathlib import Path

F = Path("meg_fetcher.py")
src = F.read_text(encoding="utf-8")
if "GROQ_FALLBACK_MODELS" in src:
    print("Gia' applicata. Nessuna modifica."); sys.exit(0)
if "GROQ_MODELS" not in src:
    print("Applica prima patch_meg_groq_cache2.py"); sys.exit(1)

REPL = [
    (r'''GROQ_MODELS = [GROQ_MODEL, os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")]''',
     r'''GROQ_MODELS = [GROQ_MODEL] + [
    m.strip() for m in os.environ.get(
        "GROQ_FALLBACK_MODELS",
        "openai/gpt-oss-120b,llama-3.3-70b-versatile,qwen/qwen3.8-27b",
    ).split(",") if m.strip()
]'''),
    (r'''                        await _groq_backoff(e.response, attempt)
                        continue
                    raise''',
     r'''                        await _groq_backoff(e.response, attempt)
                        continue
                    if e.response.status_code in (400, 404) and attempt < 4:
                        print(f"  [GROQ {e.response.status_code}] modello {_groq_model()}: "
                              f"{e.response.text[:200]}", flush=True)
                        if _groq_switch_model():
                            continue
                    raise'''),
    (r'''print(f"  [GROQ] quota giornaliera esaurita: passo al modello {GROQ_MODELS[_GROQ_MODEL_IDX]}", flush=True)''',
     r'''print(f"  [GROQ] cambio modello (quota o errore): ora {GROQ_MODELS[_GROQ_MODEL_IDX]}", flush=True)'''),
]
new = src
for old, rep in REPL:
    n = new.count(old)
    if n != 1:
        print(f"ATTENZIONE: ancora trovata {n} volte (attesa 1), nessuna modifica scritta:\n---\n{old[:150]}\n---")
        sys.exit(1)
    new = new.replace(old, rep, 1)
try:
    ast.parse(new)
except SyntaxError as e:
    print("ERRORE sintassi, nessuna modifica scritta:", e); sys.exit(1)
shutil.copy(F, "meg_fetcher.py.bak_groq3")
F.write_text(new, encoding="utf-8")
print("OK: catena modelli aggiornata:")
import re
print("  gpt-oss-20b -> gpt-oss-120b -> llama-3.3-70b-versatile -> qwen/qwen3.8-27b (404/400 e quota passano al successivo)")
