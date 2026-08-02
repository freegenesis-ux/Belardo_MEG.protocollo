#!/usr/bin/env python3
"""
Patch MEG #4: aggiunge Yahoo Finance come fonte B7 aggiuntiva (indici,
VIX, oro, EUR/USD) — endpoint pubblico, nessuna API key richiesta.
Fonte indipendente da Stooq, non soggetta allo stesso blocco anti-bot.

Idempotente. Backup .bak4.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_yahoo.py
"""

import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")
SOURCES = Path("meg_sources.yaml")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak4")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")
    changed = False

    marker1 = "async def fetch_rss_feed(client: httpx.AsyncClient, feed: dict,"
    if "async def fetch_yahoo_finance_multi(" not in text:
        insert = '''async def fetch_yahoo_finance_multi(client: httpx.AsyncClient, feed: dict,
                                     area_id: str, area_label: str,
                                     protocol: dict) -> list[dict]:
    """Fetch indici/valute/oro da Yahoo Finance (endpoint pubblico non
    ufficiale, nessuna API key richiesta). Fonte indipendente da Stooq —
    utile come fallback quando quest'ultima applica rate-limit/anti-bot.
    Un simbolo che fallisce non blocca gli altri (stesso principio di
    degradazione parziale usato per Stooq)."""
    events: list[dict] = []
    yahoo_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    for sym in feed.get("symbols", []):
        ticker = sym["yahoo"]
        url = f"{feed['url']}{ticker}?range=10d&interval=1d"
        try:
            r = await client.get(url, headers=yahoo_headers, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            result = data["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            valid = [c for c in closes if c is not None]
            if len(valid) < 2:
                continue
            last_close, prev_close = valid[-1], valid[-2]
        except Exception as e:
            record_error(f"{feed['id']}_{ticker}", area_id, e)
            continue

        pct_change = ((last_close - prev_close) / prev_close * 100) if prev_close else 0.0
        threshold_key, name = sym["key"], sym["name"]
        th_def = protocol.get("thresholds", {}).get(threshold_key, {})
        mode = "level" if threshold_key == "vix_level" else "pct"
        measured = last_close if mode == "level" else pct_change
        title = (f"{name}: {last_close:.2f} ({pct_change:+.2f}% vs seduta prec.)"
                 if mode != "level" else
                 f"{name}: {last_close:.2f} (var. giornaliera {pct_change:+.2f}%)")
        triggered, level = evaluate_threshold(measured, threshold_key, protocol)
        flags = fetcher_flags(threshold_key, round(measured, 3),
                               th_def.get("unit", ""), triggered, level,
                               notes=f"Ultima chiusura: {last_close:.2f}, "
                                     f"precedente: {prev_close:.2f} (fonte: Yahoo Finance)")
        events.append({
            "event_id":       make_event_id(area_id, title, f"{feed['id']}_{ticker}"),
            "schema_version": SCHEMA_VER,
            "extracted_at":   now_utc(),
            "meg_area":       area_id,
            "meg_area_label": area_label,
            "source":         {**source_block(feed), "id": f"{feed['id']}_{ticker}"},
            "content": {
                "title":        title,
                "summary":      f"Ultime {len(valid)} sedute disponibili su Yahoo Finance",
                "original_url": f"https://finance.yahoo.com/quote/{ticker}",
                "published_at": now_utc(),
                "language":     "en",
            },
            "meg_flags": flags,
        })
    return events


''' + marker1
        if marker1 in text:
            text = text.replace(marker1, insert, 1)
            changed = True
            print("  [OK] fetch_yahoo_finance_multi aggiunta")
        else:
            print("  [SKIP] anchor fetch_rss_feed non trovato")
    else:
        print("  [SKIP] fetch_yahoo_finance_multi già presente")

    marker2 = '    elif ftype == "stooq_multi":\n        return await fetch_stooq_multi(client, feed, area_id, area_label, protocol)\n'
    marker2_new = marker2 + '    elif ftype == "yahoo_multi":\n        return await fetch_yahoo_finance_multi(client, feed, area_id, area_label, protocol)\n'
    if 'ftype == "yahoo_multi"' not in text:
        if marker2 in text:
            text = text.replace(marker2, marker2_new, 1)
            changed = True
            print("  [OK] dispatcher aggiornato con ramo yahoo_multi")
        else:
            print("  [ATTENZIONE] anchor dispatcher stooq_multi non trovato")
    else:
        print("  [SKIP] ramo yahoo_multi già registrato")

    if changed:
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("meg_fetcher.py aggiornato.")
    else:
        print("meg_fetcher.py: nessuna modifica necessaria.")


def patch_sources():
    text = SOURCES.read_text(encoding="utf-8")
    if "yahoo_finance_multi" in text:
        print("meg_sources.yaml: fonte Yahoo già presente, nessuna modifica.")
        return

    anchor = '''    - id: stooq_snapshot
      label: Stooq Indici/Valute/Oro (storico 10gg)
      url: https://stooq.com/q/d/l/
      type: stooq_multi
      tier: 1
      symbols: ["^spx", "^vix", "^dax", "xauusd", "eurusd"]
'''
    addition = '''    - id: yahoo_finance_multi
      label: Yahoo Finance Indici/Valute/Oro (no-key, fallback)
      url: https://query1.finance.yahoo.com/v8/finance/chart/
      type: yahoo_multi
      tier: 1
      symbols:
        - { yahoo: "^GSPC",    key: "stock_index_drawdown", name: "S&P 500" }
        - { yahoo: "^VIX",     key: "vix_level",            name: "VIX" }
        - { yahoo: "^GDAXI",   key: "stock_index_drawdown", name: "DAX" }
        - { yahoo: "GC=F",     key: "gold_price_surge",     name: "Oro Futures (GC=F)" }
        - { yahoo: "EURUSD=X", key: "fx_volatility",        name: "EUR/USD" }
'''
    if anchor in text:
        text = text.replace(anchor, anchor + addition, 1)
        backup(SOURCES)
        SOURCES.write_text(text, encoding="utf-8")
        print("meg_sources.yaml aggiornato: aggiunta fonte yahoo_finance_multi")
    else:
        print("  [ATTENZIONE] blocco stooq_snapshot non trovato esattamente come atteso.")
        print("  Aggiungere manualmente la fonte yahoo_finance_multi sotto B7.")


if __name__ == "__main__":
    print("Patch meg_fetcher.py (Yahoo Finance, fallback no-key per B7)...")
    patch_fetcher()
    print()
    print("Patch meg_sources.yaml...")
    patch_sources()
    print()
    print("Verifica sintassi con:")
    print('  python3 -c "import ast; ast.parse(open(\'meg_fetcher.py\').read())"')
