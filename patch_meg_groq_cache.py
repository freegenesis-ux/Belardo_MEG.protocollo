#!/usr/bin/env python3
"""
Patch MEG: Groq 429 resilience.
 1. cache classificazioni AI (TTL 48h) -> chiama Groq solo per titoli nuovi
 2. backoff che rispetta Retry-After, log del corpo del 429, stop immediato se quota giornaliera
 3. circuit breaker: dopo un 429 definitivo, niente piu' chiamate Groq in quel run
 4. fallback_area / fallback_keywords da meg_sources.yaml quando l'AI non risponde
    (eventi marcati quality_flag=FALLBACK_NO_AI, mai area inventata)
Idempotente, con backup. Tutto-o-niente: se un'ancora non combacia, non scrive nulla.
"""
import shutil, sys
from pathlib import Path

F = Path("meg_fetcher.py")
Y = Path("meg_sources.yaml")
src = F.read_text(encoding="utf-8")
ysrc = Y.read_text(encoding="utf-8")

if "AI_CACHE_TTL_HOURS" in src:
    print("Gia' applicata (AI_CACHE_TTL_HOURS presente). Nessuna modifica.")
    sys.exit(0)

HELPERS = r'''AI_BATCH_SIZE = 20

# --- Groq resilience (patch_meg_groq_cache) ---------------------------------
AI_CACHE_TTL_HOURS = 48
GROQ_EXHAUSTED = False


def _ai_cache_path():
    import os as _os
    import pathlib as _pl
    env = _os.environ.get("MEG_AI_CACHE")
    if env:
        return _pl.Path(env).expanduser()
    return _pl.Path(__file__).resolve().parent / "meg_events" / ".ai_cache.json"


def _ai_key(title: str) -> str:
    import hashlib as _hl
    return _hl.sha1((title or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def _ai_cache_load() -> dict:
    import time as _time
    try:
        data = json.loads(_ai_cache_path().read_text(encoding="utf-8"))
        cutoff = _time.time() - AI_CACHE_TTL_HOURS * 3600
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and v.get("t", 0) >= cutoff}
    except Exception:
        return {}


def _ai_cache_save(cache: dict) -> None:
    import time as _time
    try:
        cutoff = _time.time() - AI_CACHE_TTL_HOURS * 3600
        cache = {k: v for k, v in cache.items() if v.get("t", 0) >= cutoff}
        p = _ai_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
    except Exception as e:
        print(f"  [WARN] cache AI non salvata: {e}")


def _groq_mark_failure(e) -> None:
    global GROQ_EXHAUSTED
    s = str(e)
    if "429" in s or "quota" in s.lower():
        GROQ_EXHAUSTED = True


async def _groq_backoff(resp, attempt: int) -> None:
    body = ""
    try:
        body = resp.text[:300]
    except Exception:
        pass
    print(f"  [GROQ 429] tentativo {attempt + 1}: {body}", flush=True)
    low = body.lower()
    if "per day" in low or "tpd" in low or "rpd" in low:
        raise RuntimeError("Groq quota giornaliera esaurita (429): " + body[:120])
    try:
        wait = float(resp.headers.get("retry-after"))
    except Exception:
        wait = [15, 30, 60, 65][min(attempt, 3)]
    await asyncio.sleep(min(wait, 70))


def _fallback_area_for(feed: dict, item: dict, valid_areas: dict):
    fa = feed.get("fallback_area")
    if fa in valid_areas:
        return fa
    fk = feed.get("fallback_keywords") or {}
    text = ((item.get("title") or "") + " " + (item.get("summary") or "")).lower()
    for aid, kws in fk.items():
        if aid in valid_areas and any(str(k).lower() in text for k in kws):
            return aid
    return None
# ----------------------------------------------------------------------------'''

NEW_ASSIGN = r'''    assignments: dict[int, Optional[str]] = {}
    import time as _time
    cache = _ai_cache_load()
    pending: list[int] = []
    for i, it in enumerate(items):
        ck = _ai_key(it["title"])
        if ck in cache:
            assignments[i] = cache[ck].get("a")
        else:
            pending.append(i)

    for start in range(0, len(pending), AI_BATCH_SIZE):
        idxs = pending[start:start + AI_BATCH_SIZE]
        batch = [items[i] for i in idxs]
        result = await classify_titles_ai(client, batch, valid_areas)
        for local_idx, area in result.items():
            if 0 <= local_idx < len(idxs):
                gi = idxs[local_idx]
                assignments[gi] = area
                cache[_ai_key(items[gi]["title"])] = {"a": area, "t": _time.time()}
    _ai_cache_save(cache)

    fallback_idx: set[int] = set()
    for gi in pending:
        if gi not in assignments:
            fb = _fallback_area_for(feed, items[gi], valid_areas)
            if fb:
                assignments[gi] = fb
                fallback_idx.add(gi)

    if not assignments:
        return [], "ai_unavailable"'''

OLD_ASSIGN = r'''    assignments: dict[int, Optional[str]] = {}
    for start in range(0, len(items), AI_BATCH_SIZE):
        batch = items[start:start + AI_BATCH_SIZE]
        result = await classify_titles_ai(client, batch, valid_areas)
        for local_idx, area in result.items():
            assignments[start + local_idx] = area

    if not assignments:
        return [], "ai_unavailable"'''

REPL = [
    (r'AI_BATCH_SIZE = 20', HELPERS),
    # retry loop: 5 tentativi, backoff che rispetta Retry-After
    (r'''            for attempt in range(3):
                try:
                    r = await client.post(
                        GROQ_URL,''',
     r'''            for attempt in range(5):
                try:
                    r = await client.post(
                        GROQ_URL,'''),
    (r'''                    if r.status_code == 429 and attempt < 2:
                        await asyncio.sleep(5 * (attempt + 1))''',
     r'''                    if r.status_code == 429 and attempt < 4:
                        await _groq_backoff(r, attempt)'''),
    (r'''                    if e.response.status_code == 429 and attempt < 2:
                        await asyncio.sleep(5 * (attempt + 1))''',
     r'''                    if e.response.status_code == 429 and attempt < 4:
                        await _groq_backoff(e.response, attempt)'''),
    # circuit breaker
    (r'''    area_list_txt = "\n".join(''',
     r'''    if GROQ_EXHAUSTED:
        return {}

    area_list_txt = "\n".join('''),
    (r'''        record_error("classify_titles_ai", "generalist", e)''',
     r'''        _groq_mark_failure(e)
        record_error("classify_titles_ai", "generalist", e)'''),
    # senza chiave: non uscire subito, lascia lavorare il fallback
    (r'''    if not GROQ_API_KEY:
        return [], "ai_unavailable"

    items = [{''', r'''    items = [{'''),
    (OLD_ASSIGN, NEW_ASSIGN),
    (r'''            ev["meg_flags"]["quality_flag"] = "CLASSIFICATO_AI"''',
     r'''            ev["meg_flags"]["quality_flag"] = (
                "FALLBACK_NO_AI" if i in fallback_idx else "CLASSIFICATO_AI")'''),
]

new = src
for old, rep in REPL:
    n = new.count(old)
    if n != 1:
        print(f"ATTENZIONE: ancora trovata {n} volte (attesa 1), nessuna modifica scritta:\n---\n{old[:140]}\n---")
        sys.exit(1)
    new = new.replace(old, rep, 1)

YREPL = [
    ("- id: gnews_food_security_ai\n", "- id: gnews_food_security_ai\n  fallback_area: B4_1\n"),
    ("- id: gnews_refugees_ai\n",      "- id: gnews_refugees_ai\n  fallback_area: B4_1\n"),
    ("- id: gnews_wmo_climate_ai\n",   "- id: gnews_wmo_climate_ai\n  fallback_area: B3\n"),
    ("- id: gnews_who_disease_ai\n",   "- id: gnews_who_disease_ai\n  fallback_area: B5\n"),
    ("- id: gnews_imf_econ_ai\n",      "- id: gnews_imf_econ_ai\n  fallback_area: B7\n"),
    ("- id: gdacs_ai\n",
     "- id: gdacs_ai\n  fallback_keywords:\n"
     "    B2_1_2: [earthquake, volcano, volcanic, tsunami]\n"
     "    B2_3: [flood, cyclone, hurricane, typhoon, storm, drought, wildfire, landslide]\n"),
]
ynew = ysrc
for old, rep in YREPL:
    if ynew.count(old) != 1:
        print(f"ATTENZIONE: YAML ancora non trovata: {old.strip()}  -> nessuna modifica scritta")
        sys.exit(1)
    ynew = ynew.replace(old, rep, 1)

import ast
try:
    ast.parse(new)
except SyntaxError as e:
    print("ERRORE sintassi nel risultato, nessuna modifica scritta:", e)
    sys.exit(1)

shutil.copy(F, "meg_fetcher.py.bak_groq")
shutil.copy(Y, "meg_sources.yaml.bak_groq")
F.write_text(new, encoding="utf-8")
Y.write_text(ynew, encoding="utf-8")
print("OK: meg_fetcher.py e meg_sources.yaml aggiornati (backup *.bak_groq).")

try:
    import yaml
    areas = yaml.safe_load(open("meg_protocol.yaml", encoding="utf-8")).get("macro_areas", {})
    print("\nVERIFICA MAPPA AREE (controlla che fallback_area/keywords siano coerenti):")
    for k, v in areas.items():
        print(f"  {k}: {v.get('name')}")
except Exception as e:
    print("(mappa aree non leggibile:", e, ")")
