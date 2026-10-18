#!/usr/bin/env python3
"""
Patch MEG 6 (dopo patch_meg_empty_diag.py):
 1. Un feed che risponde HTTP 202, corpo vuoto o HTML (tipico anti-bot sui runner cloud) non e'
    "empty": e' BLOCCATO -> viene registrato come errore e conta in failed_count.
 2. rigzone (bloccato dal runner con 202 vuoto) rimosso; sostituito da gnews_energy_ai
    (Google News energia/oil&gas/blackout, smistato da AI, fallback_area B6).
 3. Opzionale: --also-aljazeera-humanitarian rimuove anche aljazeera_humanitarian
    (ridondante con aljazeera_ai, filtro keywords scarta tutto).
Idempotente, tutto-o-niente, con backup.
"""
import ast, re, shutil, sys
from pathlib import Path

ALSO_AJ = "--also-aljazeera-humanitarian" in sys.argv
F = Path("meg_fetcher.py")
Y = Path("meg_sources.yaml")
src = F.read_text(encoding="utf-8")
ysrc = Y.read_text(encoding="utf-8")

# ---- 1. fetcher -------------------------------------------------------------
OLD = '''                  flush=True)
        events = []
        entries = parsed.entries
'''
NEW = '''                  flush=True)
            _ct = (r.headers.get("content-type") or "").lower()
            if r.status_code == 202 or len(r.text) == 0 or "html" in _ct:
                raise RuntimeError(
                    f"fonte BLOCCATA (HTTP {r.status_code}, {len(r.text)} byte, {_ct or 'no content-type'}): "
                    "probabile anti-bot sui runner cloud, non un feed vuoto")
        events = []
        entries = parsed.entries
'''
fetcher_new = src
if "fonte BLOCCATA" not in src:
    if "[EMPTY]" not in src:
        print("Applica prima patch_meg_empty_diag.py"); sys.exit(1)
    n = src.count(OLD)
    if n != 1:
        print(f"ATTENZIONE: ancora fetcher trovata {n} volte (attesa 1), nessuna modifica scritta"); sys.exit(1)
    fetcher_new = src.replace(OLD, NEW, 1)
    try:
        ast.parse(fetcher_new)
    except SyntaxError as e:
        print("ERRORE sintassi, nessuna modifica scritta:", e); sys.exit(1)


# ---- 2/3. yaml --------------------------------------------------------------
def remove_block(text: str, fid: str):
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)- id:\s*" + re.escape(fid) + r"\s*$", ln.rstrip("\n"))
        if m:
            start, ind = i, len(m.group(1))
            break
    if start is None:
        return text, False
    end = start + 1
    while end < len(lines):
        s = lines[end]
        if s.strip() and (len(s) - len(s.lstrip())) <= ind:
            break
        end += 1
    return "".join(lines[:start] + lines[end:]), True


NEW_SRC = '''- id: gnews_energy_ai
  fallback_area: B6
  label: Google News - Energia/Oil&Gas/Blackout (smistato da AI, sostituisce Rigzone bloccato)
  url: https://news.google.com/rss/search?q=oil+OR+%22natural+gas%22+OR+OPEC+OR+LNG+OR+blackout+OR+%22power+outage%22+OR+%22energy+crisis%22&hl=en-US&gl=US&ceid=US:en
  type: rss
  tier: 2
'''
ynew = ysrc
msgs = []
ynew, ok = remove_block(ynew, "rigzone")
msgs.append("rigzone rimosso" if ok else "rigzone: gia' assente")
if ALSO_AJ:
    ynew, ok = remove_block(ynew, "aljazeera_humanitarian")
    msgs.append("aljazeera_humanitarian rimosso" if ok else "aljazeera_humanitarian: gia' assente")
if "id: gnews_energy_ai" not in ynew:
    anchor = "- id: gnews_imf_econ_ai\n"
    if ynew.count(anchor) != 1:
        print("ATTENZIONE: ancora '- id: gnews_imf_econ_ai' non trovata una sola volta, nessuna modifica scritta"); sys.exit(1)
    ynew = ynew.replace(anchor, NEW_SRC + anchor, 1)
    msgs.append("gnews_energy_ai aggiunta")
try:
    import yaml
    d = yaml.safe_load(ynew)
    ids = []
    def walk(x):
        if isinstance(x, dict):
            if "id" in x and "url" in x: ids.append(x["id"])
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(d)
    assert "gnews_energy_ai" in ids and "rigzone" not in ids, "yaml risultante incoerente"
    assert len(ids) == len(set(ids)), "id duplicati nel yaml"
except Exception as e:
    print("ERRORE validazione yaml, nessuna modifica scritta:", e); sys.exit(1)

if fetcher_new != src:
    shutil.copy(F, "meg_fetcher.py.bak_blocked")
    F.write_text(fetcher_new, encoding="utf-8")
    msgs.append("fetcher: feed 202/vuoti/HTML -> BLOCCATA")
if ynew != ysrc:
    shutil.copy(Y, "meg_sources.yaml.bak_blocked")
    Y.write_text(ynew, encoding="utf-8")
print("OK:", "; ".join(msgs))

# altri riferimenti a rigzone nel repo (informativo)
import subprocess
try:
    out = subprocess.run(["grep", "-rIl", "-i", "rigzone", ".", "--exclude-dir=.git", "--exclude-dir=archive",
                          "--exclude=*.bak*", "--exclude=*.jsonl", "--exclude=patch_meg_*"],
                         capture_output=True, text=True).stdout.strip()
    if out:
        print("Altri file che citano rigzone (controlla se serve aggiornarli):\n" + out)
except Exception:
    pass
