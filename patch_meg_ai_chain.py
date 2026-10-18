#!/usr/bin/env python3
"""
Patch MEG 4 (dopo patch_meg_groq_models.py):
 1. BUG cache: i feed girano in parallelo, ognuno caricava la cache all'inizio e la risalvava alla fine:
    l'ultimo a salvare cancellava le voci degli altri (nel run reale: 25 titoli invece di centinaia).
    Ora la cache e' una sola, condivisa nel processo.
 2. BUG visibilita': se un batch AI falliva e il feed non aveva fallback, i titoli sparivano e lo stato restava "ok".
    Ora conta come degraded_fallback.
 3. PARACADUTE: catena multi-provider (stesso formato chat/completions):
      Groq (gpt-oss-20b, gpt-oss-120b, llama-3.3-70b, qwen) -> Gemini (se GEMINI_API_KEY) -> OpenRouter free (se OPENROUTER_API_KEY)
    Su quota esaurita, 404/400, o 3 rate-limit al minuto di fila, passa al successivo.
    Parsing JSON tollerante (code fence, <think>).
 4. Workflow: passa GEMINI_API_KEY e OPENROUTER_API_KEY dai secret (vuoti = provider ignorato).
Idempotente, tutto-o-niente, con backup.
"""
import ast, re, shutil, sys
from pathlib import Path

F = Path("meg_fetcher.py")
W = Path(".github/workflows/meg_fetch.yml")
src = F.read_text(encoding="utf-8")
if "_AI_CHAIN" in src:
    print("Gia' applicata. Nessuna modifica."); sys.exit(0)
if "GROQ_FALLBACK_MODELS" not in src:
    print("Applica prima patch_meg_groq_models.py"); sys.exit(1)

OLD_MODELS = r'''GROQ_MODELS = [GROQ_MODEL] + [
    m.strip() for m in os.environ.get(
        "GROQ_FALLBACK_MODELS",
        "openai/gpt-oss-120b,llama-3.3-70b-versatile,qwen/qwen3.8-27b",
    ).split(",") if m.strip()
]
_GROQ_MODEL_IDX = 0


def _groq_model() -> str:
    return GROQ_MODELS[_GROQ_MODEL_IDX]


def _groq_switch_model() -> bool:
    global _GROQ_MODEL_IDX
    if _GROQ_MODEL_IDX + 1 < len(GROQ_MODELS):
        _GROQ_MODEL_IDX += 1
        print(f"  [GROQ] cambio modello (quota o errore): ora {GROQ_MODELS[_GROQ_MODEL_IDX]}", flush=True)
        return True
    return False
'''
NEW_MODELS = r'''GROQ_FALLBACK_MODELS = os.environ.get(
    "GROQ_FALLBACK_MODELS",
    "openai/gpt-oss-120b,llama-3.3-70b-versatile,qwen/qwen3.8-27b",
)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _csv(s: str) -> list:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _build_ai_chain() -> list:
    """Catena ordinata (provider, url, chiave, modello). Provider senza chiave: ignorato."""
    chain = []
    if GROQ_API_KEY:
        for m in [GROQ_MODEL] + _csv(GROQ_FALLBACK_MODELS):
            chain.append({"provider": "groq", "url": GROQ_URL, "key": GROQ_API_KEY, "model": m})
    gk = os.environ.get("GEMINI_API_KEY")
    if gk:
        for m in _csv(os.environ.get("GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.5-flash")):
            chain.append({"provider": "gemini", "url": GEMINI_URL, "key": gk, "model": m})
    ok = os.environ.get("OPENROUTER_API_KEY")
    if ok:
        for m in _csv(os.environ.get("OPENROUTER_MODELS",
                                     "openai/gpt-oss-20b:free,meta-llama/llama-3.3-70b-instruct:free")):
            chain.append({"provider": "openrouter", "url": OPENROUTER_URL, "key": ok, "model": m})
    return chain


_AI_CHAIN = _build_ai_chain()
_GROQ_MODEL_IDX = 0
_AI_CACHE_SHARED = None
_AI_WAITS = 0


def _ai_cur() -> dict:
    return _AI_CHAIN[_GROQ_MODEL_IDX]


def _groq_model() -> str:
    return _ai_cur()["model"]


def _groq_switch_model() -> bool:
    global _GROQ_MODEL_IDX, _AI_WAITS
    if _GROQ_MODEL_IDX + 1 < len(_AI_CHAIN):
        _GROQ_MODEL_IDX += 1
        _AI_WAITS = 0
        c = _ai_cur()
        print(f"  [AI] cambio (quota o errore): ora {c['provider']}/{c['model']}", flush=True)
        return True
    return False


def _ai_cache_shared() -> dict:
    """Una sola cache per processo: i feed paralleli non si sovrascrivono a vicenda."""
    global _AI_CACHE_SHARED
    if _AI_CACHE_SHARED is None:
        _AI_CACHE_SHARED = _ai_cache_load()
    return _AI_CACHE_SHARED


def _ai_parse_json(content: str):
    import re as _re
    t = _re.sub(r"<think>.*?</think>", "", content or "", flags=_re.DOTALL)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < i:
        raise ValueError("risposta AI senza JSON")
    return json.loads(t[i:j + 1])
'''

REPL = [
    (OLD_MODELS, NEW_MODELS),
    # classify: nessun provider configurato
    (r'''    if not GROQ_API_KEY:
        return {}

    if GROQ_EXHAUSTED:''', r'''    if not _AI_CHAIN:
        return {}

    if GROQ_EXHAUSTED:'''),
    # url + chiave correnti
    (r'''                        GROQ_URL,
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},''',
     r'''                        _ai_cur()["url"],
                        headers={"Authorization": f"Bearer {_ai_cur()['key']}"},'''),
    # parametri specifici per provider
    (r'''**({"reasoning_effort": "low"} if "gpt-oss" in _groq_model() else {}),''',
     r'''**({"reasoning_effort": "low"}
                               if _ai_cur()["provider"] == "groq" and "gpt-oss" in _groq_model() else {}),'''),
    (r'''        raw = json.loads(content)''', r'''        raw = _ai_parse_json(content)'''),
    # backoff: parole chiave quota giornaliera di tutti i provider + cambio dopo 2 attese
    (r'''    if "per day" in low or "tpd" in low or "rpd" in low:''',
     r'''    if any(w in low for w in ("per day", "per-day", "perday", "tpd", "rpd")):'''),
    (r'''    try:
        wait = float(resp.headers.get("retry-after"))''',
     r'''    global _AI_WAITS
    _AI_WAITS += 1
    if _AI_WAITS > 2 and _groq_switch_model():
        return
    try:
        wait = float(resp.headers.get("retry-after"))'''),
    (r'''wait = [15, 30, 60, 65][min(attempt, 3)]''',
     r'''wait = [15, 30, 60][min(_AI_WAITS - 1, 2)]'''),
    # tentativi per chiamata: abbastanza da attraversare tutta la catena
    (r'''            for attempt in range(5):
                try:
                    r = await client.post(''',
     r'''            for attempt in range(len(_AI_CHAIN) + 4):
                try:
                    r = await client.post('''),
    # cache condivisa
    (r'''    cache = _ai_cache_load()''', r'''    cache = _ai_cache_shared()'''),
    # perdite silenziose -> degraded
    (r'''    if not assignments:
        return [], "ai_unavailable"

    events = []''', r'''    lost_idx = [i for i in pending if i not in assignments]
    if not assignments:
        return [], "ai_unavailable"

    events = []'''),
    (r'''    if events and fallback_idx:''', r'''    if events and (fallback_idx or lost_idx):'''),
]
# parametri opzionali presenti solo nel codice reale
OPTIONAL = [
    (r'''"response_format": {"type": "json_object"},''',
     r'''**({"response_format": {"type": "json_object"}} if _ai_cur()["provider"] == "groq" else {}),'''),
    (r'''"max_completion_tokens": 4096,''',
     r'''**({"max_completion_tokens": 4096} if _ai_cur()["provider"] == "groq" else {"max_tokens": 4096}),'''),
]

new = src
if new.count("attempt < 4") != 3:
    print(f"ATTENZIONE: 'attempt < 4' trovato {new.count('attempt < 4')} volte (attese 3), nessuna modifica scritta"); sys.exit(1)
new = new.replace("attempt < 4", "attempt < len(_AI_CHAIN) + 3")
for old, rep in REPL:
    n = new.count(old)
    if n != 1:
        print(f"ATTENZIONE: ancora trovata {n} volte (attesa 1), nessuna modifica scritta:\n---\n{old[:160]}\n---")
        sys.exit(1)
    new = new.replace(old, rep, 1)
for old, rep in OPTIONAL:
    n = new.count(old)
    if n == 1:
        new = new.replace(old, rep, 1)
    else:
        print(f"ATTENZIONE: parametro opzionale trovato {n} volte, non modificato:\n  {old}")
        sys.exit(1)
try:
    ast.parse(new)
except SyntaxError as e:
    print("ERRORE sintassi, nessuna modifica scritta:", e); sys.exit(1)

# workflow: aggiunge i secret accanto a GROQ_API_KEY
wnew = None
if W.exists():
    wl = W.read_text(encoding="utf-8").splitlines(keepends=True)
    out, added = [], 0
    for ln in wl:
        out.append(ln)
        m = re.match(r"^(\s*)GROQ_API_KEY:\s*\$\{\{\s*secrets\.GROQ_API_KEY\s*\}\}\s*$", ln.rstrip("\n"))
        if m:
            ind = m.group(1)
            out.append(f"{ind}GEMINI_API_KEY: ${{{{ secrets.GEMINI_API_KEY }}}}\n")
            out.append(f"{ind}OPENROUTER_API_KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}\n")
            added += 1
    wnew = "".join(out)
    print(f"workflow: aggiunte righe GEMINI/OPENROUTER in {added} step")
    if added == 0:
        print("ATTENZIONE: riga 'GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}' non trovata nel workflow; nessuna modifica scritta.")
        sys.exit(1)

shutil.copy(F, "meg_fetcher.py.bak_groq4")
F.write_text(new, encoding="utf-8")
if wnew:
    shutil.copy(W, "meg_fetch.yml.bak_groq4")
    W.write_text(wnew, encoding="utf-8")
print("OK: meg_fetcher.py aggiornato (catena AI, cache condivisa, perdite visibili).")
print("Provider attivi dipendono dai secret: GROQ_API_KEY (c'e'), GEMINI_API_KEY e OPENROUTER_API_KEY (da creare, opzionali).")
