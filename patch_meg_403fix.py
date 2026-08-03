#!/usr/bin/env python3
"""
Patch MEG #6: le fonti che rispondono 403 Forbidden (wmo_news, unhcr_news,
ipc_alerts, imf_news, gse_it) bloccano quasi certamente lo User-Agent
generico MEGFetcher/3.0. Sostituito con uno User-Agent browser realistico
in USER_AGENTS — stessa soluzione già collaudata oggi su Stooq/Yahoo.

Inoltre: fao_news (404, URL RSS dismesso) sostituita con il feed
ReliefWeb filtrato per organizzazione (source=2836, verificato).

Idempotente. Backup .bak6.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_403fix.py
"""

import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")
SOURCES = Path("meg_sources.yaml")

def backup(path: Path, suffix=".bak6"):
    bak = path.with_suffix(path.suffix + suffix)
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")

    old = '''USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MEGFetcher/3.0",
    "MEGFetcher/3.0 (+monitoraggio eventi globali; contatto operatore)",
]'''
    new = '''USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]'''
    if old in text:
        text = text.replace(old, new, 1)
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("  [OK] USER_AGENTS sostituiti con header browser realistici")
    elif "Chrome/120.0.0.0 Safari/537.36" in text:
        print("  [SKIP] USER_AGENTS già aggiornati")
    else:
        print("  [ATTENZIONE] blocco USER_AGENTS non trovato esattamente come atteso")


def patch_sources():
    text = SOURCES.read_text(encoding="utf-8")

    old = '''    - id: fao_news
      label: FAO News
      url: https://www.fao.org/news/rss-news-archive/rss-detail/en/
      type: rss
      tier: 1'''
    new = '''    - id: fao_news
      label: FAO News (via ReliefWeb)
      url: "https://reliefweb.int/updates/rss.xml?advanced-search=%28S2836%29"
      type: rss
      tier: 1'''
    if old in text:
        text = text.replace(old, new, 1)
        backup(SOURCES)
        SOURCES.write_text(text, encoding="utf-8")
        print("  [OK] fao_news reindirizzata su ReliefWeb (source=2836)")
    elif "S2836" in text:
        print("  [SKIP] fao_news già aggiornata")
    else:
        print("  [ATTENZIONE] blocco fao_news non trovato esattamente come atteso")


if __name__ == "__main__":
    print("Patch User-Agent (risolve i 403) + fix FAO via ReliefWeb...")
    patch_fetcher()
    patch_sources()
    print()
    print("Verifica sintassi con:")
    print('  python3 -c "import ast; ast.parse(open(\'meg_fetcher.py\').read())"')
