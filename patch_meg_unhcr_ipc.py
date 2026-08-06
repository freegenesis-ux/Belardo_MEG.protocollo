#!/usr/bin/env python3
"""
Patch MEG #8: unhcr_news e ipc_alerts bloccati con 403 persistente anche
con User-Agent browser — WAF che va oltre il semplice controllo header.
Sostituiti con feed ReliefWeb filtrati per organizzazione (stesso
pattern già verificato con successo per FAO/WFP):
  - UNHCR = ReliefWeb source S2868
  - IPC   = ReliefWeb source S3495

Idempotente. Backup .bak8.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_unhcr_ipc.py
"""

import shutil
from pathlib import Path

SOURCES = Path("meg_sources.yaml")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak8")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_sources():
    text = SOURCES.read_text(encoding="utf-8")
    changed = False

    old_unhcr = '''    - id: unhcr_news
      label: UNHCR News
      url: https://www.unhcr.org/rss.xml
      type: rss
      tier: 1'''
    new_unhcr = '''    - id: unhcr_news
      label: UNHCR News (via ReliefWeb)
      url: "https://reliefweb.int/updates/rss.xml?advanced-search=%28S2868%29"
      type: rss
      tier: 1'''
    if old_unhcr in text:
        text = text.replace(old_unhcr, new_unhcr, 1)
        changed = True
        print("  [OK] unhcr_news reindirizzata su ReliefWeb (source=2868)")
    elif "S2868" in text:
        print("  [SKIP] unhcr_news già aggiornata")
    else:
        print("  [ATTENZIONE] blocco unhcr_news non trovato esattamente — verificare a mano")

    old_ipc = '''    - id: ipc_alerts
      label: IPC Food Security
      url: https://www.ipcinfo.org/ipcinfo-website/news-archive/news/en/rss
      type: rss
      tier: 1'''
    new_ipc = '''    - id: ipc_alerts
      label: IPC Food Security (via ReliefWeb)
      url: "https://reliefweb.int/updates/rss.xml?advanced-search=%28S3495%29"
      type: rss
      tier: 1'''
    if old_ipc in text:
        text = text.replace(old_ipc, new_ipc, 1)
        changed = True
        print("  [OK] ipc_alerts reindirizzata su ReliefWeb (source=3495)")
    elif "S3495" in text:
        print("  [SKIP] ipc_alerts già aggiornata")
    else:
        print("  [ATTENZIONE] blocco ipc_alerts non trovato esattamente — verificare a mano")

    if changed:
        backup(SOURCES)
        SOURCES.write_text(text, encoding="utf-8")
        print("meg_sources.yaml aggiornato.")
    else:
        print("meg_sources.yaml: nessuna modifica necessaria.")


if __name__ == "__main__":
    print("Patch UNHCR + IPC via ReliefWeb...")
    patch_sources()
