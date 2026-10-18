#!/usr/bin/env python3
"""
Patch MEG 5: diagnostica dei feed RSS che rispondono ma restituiscono 0 voci.
Quando feedparser non trova voci (e senza errore), stampa nel log: HTTP, byte, content-type
e l'inizio del corpo. Cosi' si distingue "feed vuoto" da "pagina anti-bot / challenge / HTML".
Idempotente, tutto-o-niente, con backup.
"""
import ast, shutil, sys
from pathlib import Path

F = Path("meg_fetcher.py")
src = F.read_text(encoding="utf-8")
if "[EMPTY]" in src:
    print("Gia' applicata. Nessuna modifica."); sys.exit(0)

OLD = '''            parsed = feedparser.parse(r.text)
        events = []
        entries = parsed.entries
'''
NEW = '''            parsed = feedparser.parse(r.text)
        if not parsed.entries:
            print(f"  [EMPTY] {feed.get('id')}: HTTP {r.status_code}, {len(r.text)} byte, "
                  f"content-type={r.headers.get('content-type')}, inizio: {r.text[:160]!r}",
                  flush=True)
        events = []
        entries = parsed.entries
'''
n = src.count(OLD)
if n != 1:
    print(f"ATTENZIONE: ancora trovata {n} volte (attesa 1), nessuna modifica scritta"); sys.exit(1)
new = src.replace(OLD, NEW, 1)
try:
    ast.parse(new)
except SyntaxError as e:
    print("ERRORE sintassi, nessuna modifica scritta:", e); sys.exit(1)
shutil.copy(F, "meg_fetcher.py.bak_diag")
F.write_text(new, encoding="utf-8")
print("OK: diagnostica [EMPTY] aggiunta a fetch_rss_feed (backup meg_fetcher.py.bak_diag).")
