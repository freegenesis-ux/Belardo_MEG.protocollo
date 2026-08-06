#!/usr/bin/env python3
"""
Patch MEG #3: Stooq ha introdotto una challenge anti-bot proof-of-work
(SHA-256 con N zeri iniziali, verificata via POST a /__verify) che blocca
il fetch diretto del CSV storico usato per B7 (VIX, S&P, oro, EUR/USD).

Questa patch risolve la challenge computazionalmente (stesso calcolo che
farebbe un browser eseguendo il JS, replicato in Python — nessun bypass
di CAPTCHA o verifica umana, solo hashing) e riusa il cookie di sessione
ottenuto per tutte le richieste successive nello stesso client.

Idempotente. Backup .bak3.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_stooq.py
"""

import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak3")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")
    changed = False

    # ── 1. Funzione solve_stooq_challenge, prima di fetch_stooq_multi ──
    marker1 = "async def fetch_stooq_multi(client: httpx.AsyncClient, feed: dict,"
    if "def solve_stooq_challenge(" not in text:
        insert = '''async def solve_stooq_challenge(client: httpx.AsyncClient, base_url: str) -> None:
    """Stooq protegge le richieste automatizzate con una challenge PoW
    lato client (SHA-256 con N zeri iniziali, verificata via POST a
    /__verify). Risolta qui con lo stesso calcolo che farebbe un browser
    eseguendo il JS — puro hashing, nessun bypass di verifica umana. Il
    cookie di sessione ottenuto resta valido nel client per le richieste
    successive allo stesso dominio (nessuna azione richiesta al chiamante)."""
    import hashlib
    import re

    try:
        r = await client.get(base_url, timeout=FETCH_TIMEOUT)
    except Exception:
        return  # se anche il probe fallisce, i fetch successivi falliranno e verranno loggati normalmente

    if "requires JavaScript to verify" not in r.text:
        return  # nessuna challenge attiva in questo momento

    m = re.search(r'c="([^"]+)"\\s*,\\s*d=(\\d+)', r.text)
    if not m:
        return  # pattern challenge cambiato — richiede aggiornamento manuale

    c, d = m.group(1), int(m.group(2))
    target = "0" * d
    n = 0
    while True:
        h = hashlib.sha256(f"{c}{n}".encode()).hexdigest()
        if h.startswith(target):
            break
        n += 1
        if n > 2_000_000:  # safety valve — non deve mai servire con d<=6
            return

    origin = base_url.split("/q/")[0].split("/db/")[0]
    if not origin.startswith("http"):
        origin = "https://stooq.com"
    try:
        await client.post(f"{origin}/__verify",
                           data={"c": c, "n": str(n)},
                           timeout=FETCH_TIMEOUT)
    except Exception:
        return  # cookie non ottenuto — i fetch successivi falliranno e verranno loggati normalmente


''' + marker1
        if marker1 in text:
            text = text.replace(marker1, insert, 1)
            changed = True
            print("  [OK] solve_stooq_challenge aggiunta")
        else:
            print("  [SKIP] anchor fetch_stooq_multi non trovato")
    else:
        print("  [SKIP] solve_stooq_challenge già presente")

    # ── 2. Chiama la challenge-solve una volta, prima del loop sui simboli ──
    marker2 = '''    symbols = feed.get("symbols", [])
    events: list[dict] = []
    d2 = datetime.datetime.utcnow()
    d1 = d2 - datetime.timedelta(days=14)
    d1s, d2s = d1.strftime("%Y%m%d"), d2.strftime("%Y%m%d")

    for symbol in symbols:'''
    marker2_new = '''    symbols = feed.get("symbols", [])
    events: list[dict] = []
    d2 = datetime.datetime.utcnow()
    d1 = d2 - datetime.timedelta(days=14)
    d1s, d2s = d1.strftime("%Y%m%d"), d2.strftime("%Y%m%d")

    # Risolve la eventuale challenge PoW una sola volta per l'intero batch
    # di simboli — il cookie ottenuto resta valido nel client condiviso.
    await solve_stooq_challenge(client, feed["url"])

    for symbol in symbols:'''
    if marker2 in text:
        text = text.replace(marker2, marker2_new, 1)
        changed = True
        print("  [OK] chiamata solve_stooq_challenge integrata nel loop simboli")
    elif "await solve_stooq_challenge(client, feed[" in text:
        print("  [SKIP] chiamata già integrata")
    else:
        print("  [ATTENZIONE] anchor loop simboli non trovato — verificare a mano")

    if changed:
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("meg_fetcher.py aggiornato.")
    else:
        print("meg_fetcher.py: nessuna modifica necessaria (già patchato o anchor mancanti).")


if __name__ == "__main__":
    print("Patch meg_fetcher.py (bypass PoW Stooq per B7)...")
    patch_fetcher()
    print()
    print("Verifica sintassi con:")
    print('  python3 -c "import ast; ast.parse(open(\'meg_fetcher.py\').read())"')
