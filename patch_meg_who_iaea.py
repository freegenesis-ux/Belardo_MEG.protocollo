#!/usr/bin/env python3
"""
Patch MEG #9: who_disease e iaea_news (404, URL RSS dismessi) sostituiti
con feed ReliefWeb filtrati per organizzazione (stesso pattern verificato
con successo per FAO/WFP/UNHCR/IPC):
  - WHO  = ReliefWeb source S1275
  - IAEA = ReliefWeb source S1002

Idempotente. Backup .bak9.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_who_iaea.py
"""

import shutil
from pathlib import Path

SOURCES = Path("meg_sources.yaml")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak9")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_sources():
    text = SOURCES.read_text(encoding="utf-8")
    changed = False

    old_who = '''    - id: who_disease
      label: WHO Disease Outbreaks
      url: https://www.who.int/feeds/entity/csr/don/en/rss.xml
      type: rss
      tier: 1'''
    new_who = '''    - id: who_disease
      label: WHO Disease Outbreaks (via ReliefWeb)
      url: "https://reliefweb.int/updates/rss.xml?advanced-search=%28S1275%29"
      type: rss
      tier: 1'''
    if old_who in text:
        text = text.replace(old_who, new_who, 1)
        changed = True
        print("  [OK] who_disease reindirizzata su ReliefWeb (source=1275)")
    elif "S1275" in text:
        print("  [SKIP] who_disease già aggiornata")
    else:
        print("  [ATTENZIONE] blocco who_disease non trovato esattamente — verificare a mano")

    old_iaea = '''    - id: iaea_news
      label: IAEA News
      url: https://www.iaea.org/feeds/topstories.rss
      type: rss
      tier: 1'''
    new_iaea = '''    - id: iaea_news
      label: IAEA News (via ReliefWeb)
      url: "https://reliefweb.int/updates/rss.xml?advanced-search=%28S1002%29"
      type: rss
      tier: 1'''
    if old_iaea in text:
        text = text.replace(old_iaea, new_iaea, 1)
        changed = True
        print("  [OK] iaea_news reindirizzata su ReliefWeb (source=1002)")
    elif "S1002" in text:
        print("  [SKIP] iaea_news già aggiornata")
    else:
        print("  [ATTENZIONE] blocco iaea_news non trovato esattamente — verificare a mano")

    if changed:
        backup(SOURCES)
        SOURCES.write_text(text, encoding="utf-8")
        print("meg_sources.yaml aggiornato.")
    else:
        print("meg_sources.yaml: nessuna modifica necessaria.")


if __name__ == "__main__":
    print("Patch WHO + IAEA via ReliefWeb...")
    patch_sources()
