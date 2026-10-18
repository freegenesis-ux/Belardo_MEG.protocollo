#!/usr/bin/env python3
"""
Patch MEG 2 (dopo patch_meg_groq_cache.py):
 - controllo "quota esaurita" DENTRO il semaforo (i feed paralleli non sprecano piu' chiamate)
 - cambio modello automatico quando il limite giornaliero (TPD) del modello corrente e' esaurito
   (i limiti Groq sono per modello): GROQ_MODELS = [gpt-oss-20b, GROQ_FALLBACK_MODEL]
 - riassunto nel prompt 200 -> 100 caratteri (meno token)
 - source_status: ai_unavailable conta in failed_count; nuovo stato degraded_fallback + degraded_count
Idempotente, tutto-o-niente, con backup.
"""
import ast, shutil, sys
from pathlib import Path

F = Path("meg_fetcher.py")
src = F.read_text(encoding="utf-8")
if "GROQ_MODELS" in src:
    print("Gia' applicata. Nessuna modifica.")
    sys.exit(0)
if "AI_CACHE_TTL_HOURS" not in src:
    print("Applica prima patch_meg_groq_cache.py"); sys.exit(1)

REPL = [
    # modelli
    (r'''GROQ_EXHAUSTED = False
''', r'''GROQ_EXHAUSTED = False
GROQ_MODELS = [GROQ_MODEL, os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")]
_GROQ_MODEL_IDX = 0


def _groq_model() -> str:
    return GROQ_MODELS[_GROQ_MODEL_IDX]


def _groq_switch_model() -> bool:
    global _GROQ_MODEL_IDX
    if _GROQ_MODEL_IDX + 1 < len(GROQ_MODELS):
        _GROQ_MODEL_IDX += 1
        print(f"  [GROQ] quota giornaliera esaurita: passo al modello {GROQ_MODELS[_GROQ_MODEL_IDX]}", flush=True)
        return True
    return False
'''),
    (r'''    if "per day" in low or "tpd" in low or "rpd" in low:
        raise RuntimeError(''', r'''    if "per day" in low or "tpd" in low or "rpd" in low:
        if _groq_switch_model():
            return
        raise RuntimeError('''),
    (r'''                            "model": GROQ_MODEL,''',
     r'''                            "model": _groq_model(),'''),
    (r'''                            "reasoning_effort": "low",''',
     r'''                            **({"reasoning_effort": "low"} if "gpt-oss" in _groq_model() else {}),'''),
    # controllo dentro il semaforo
    (r'''        async with GROQ_SEMAPHORE:
            for attempt in range(5):''',
     r'''        async with GROQ_SEMAPHORE:
            if GROQ_EXHAUSTED:
                return {}
            for attempt in range(5):'''),
    # meno token nel prompt
    (r'''(it.get('summary') or '')[:200]''', r'''(it.get('summary') or '')[:100]'''),
    # stati
    (r'''    return events, ("ok" if events else "empty")


async def fetch_usgs(''', r'''    if events and fallback_idx:
        return events, "degraded_fallback"
    return events, ("ok" if events else "empty")


async def fetch_usgs('''),
    (r'''"failed_count": sum(1 for s in source_status if s["status"] == "error"),
            "empty_count":  sum(1 for s in source_status if s["status"] == "empty"),''',
     r'''"failed_count": sum(1 for s in source_status if s["status"] in ("error", "ai_unavailable")),
            "degraded_count": sum(1 for s in source_status if s["status"] == "degraded_fallback"),
            "empty_count":  sum(1 for s in source_status if s["status"] == "empty"),'''),
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
shutil.copy(F, "meg_fetcher.py.bak_groq2")
F.write_text(new, encoding="utf-8")
print("OK: meg_fetcher.py aggiornato (backup meg_fetcher.py.bak_groq2).")
