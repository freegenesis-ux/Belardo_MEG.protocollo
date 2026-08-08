#!/usr/bin/env python3
"""
Patch MEG #10: ReliefWeb applica rate-limit quando riceve più richieste
simultanee dallo stesso IP — confermato: le 6 fonti reindirizzate su
ReliefWeb (FAO/WFP/UNHCR/IPC/WHO/IAEA) funzionano perfettamente se
testate in sequenza, ma risultano vuote nel run 'full' dove
asyncio.gather() le lancia tutte insieme.

Fix: un semaforo dedicato serializza solo le chiamate verso
reliefweb.int, lasciando invariata la concorrenza di tutte le altre
fonti (che non hanno questo problema).

Idempotente. Backup .bak10.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_reliefweb_throttle.py
"""

import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak10")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")
    changed = False

    marker1 = "async def fetch_with_retry(client: httpx.AsyncClient, url: str,"
    if "RELIEFWEB_SEMAPHORE" not in text:
        insert = '''# ReliefWeb applica rate-limit su richieste simultanee dallo stesso IP —
# questo semaforo serializza solo le chiamate verso reliefweb.int, senza
# rallentare la concorrenza delle altre fonti (che non hanno il problema).
RELIEFWEB_SEMAPHORE = asyncio.Semaphore(1)


''' + marker1
        if marker1 in text:
            text = text.replace(marker1, insert, 1)
            changed = True
            print("  [OK] RELIEFWEB_SEMAPHORE aggiunto")
        else:
            print("  [SKIP] anchor fetch_with_retry non trovato")
    else:
        print("  [SKIP] RELIEFWEB_SEMAPHORE già presente")

    marker2 = '''async def fetch_rss_feed(client: httpx.AsyncClient, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> list[dict]:
    try:
        r = await fetch_with_retry(client, feed["url"])'''
    marker2_new = '''async def fetch_rss_feed(client: httpx.AsyncClient, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> list[dict]:
    is_reliefweb = "reliefweb.int" in feed.get("url", "")
    try:
        if is_reliefweb:
            async with RELIEFWEB_SEMAPHORE:
                r = await fetch_with_retry(client, feed["url"])
                await asyncio.sleep(1.5)  # margine di cortesia tra una richiesta e la successiva
        else:
            r = await fetch_with_retry(client, feed["url"])'''
    if marker2 in text:
        text = text.replace(marker2, marker2_new, 1)
        changed = True
        print("  [OK] fetch_rss_feed aggiornata con throttling ReliefWeb")
    elif "is_reliefweb = " in text:
        print("  [SKIP] fetch_rss_feed già patchata")
    else:
        print("  [ATTENZIONE] anchor fetch_rss_feed (corpo) non trovato — verificare a mano")

    if changed:
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("meg_fetcher.py aggiornato.")
    else:
        print("meg_fetcher.py: nessuna modifica necessaria.")


if __name__ == "__main__":
    print("Patch throttling ReliefWeb (semaforo dedicato)...")
    patch_fetcher()
    print()
    print("Verifica sintassi con:")
    print('  python3 -c "import ast; ast.parse(open(\'meg_fetcher.py\').read())"')
