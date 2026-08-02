#!/usr/bin/env python3
"""
Patch MEG #5: aggiunge Alpha Vantage come fonte B7 aggiuntiva (S&P 500
via SPY, oro via GLD, EUR/USD via FX_DAILY) — richiede API key gratuita
in ALPHAVANTAGE_KEY (env var / GitHub secret). Se la chiave non è
impostata, la fonte si auto-esclude senza errore (nessuna riga in output,
non "error" nel source_status).

Rate limit free tier: 25 richieste/giorno. Con 3 simboli per run, throttling
necessario: gira solo se sono passate almeno ALPHAVANTAGE_MIN_HOURS ore
dall'ultimo fetch riuscito (persistito in meg_events/.alphavantage_last_run).

Idempotente. Backup .bak5.

Uso:
    cd ~/Belardo_MEG.protocollo
    python3 patch_meg_alphavantage.py
"""

import shutil
from pathlib import Path

FETCHER = Path("meg_fetcher.py")
SOURCES = Path("meg_sources.yaml")

def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".bak5")
    if not bak.exists():
        shutil.copy(path, bak)
        print(f"  backup creato: {bak}")

def patch_fetcher():
    text = FETCHER.read_text(encoding="utf-8")
    changed = False

    marker1 = "async def fetch_rss_feed(client: httpx.AsyncClient, feed: dict,"
    if "async def fetch_alphavantage_multi(" not in text:
        insert = '''ALPHAVANTAGE_THROTTLE_FILE = OUTPUT_DIR / ".alphavantage_last_run"
ALPHAVANTAGE_MIN_HOURS = 3  # 3 simboli x 8 run/giorno = 24 richieste, sotto il limite di 25/giorno

def alphavantage_should_run() -> bool:
    """Throttling persistente su file — evita di sforare il rate limit
    gratuito (25 richieste/giorno) quando il workflow gira ogni ora."""
    if not ALPHAVANTAGE_THROTTLE_FILE.exists():
        return True
    try:
        last = datetime.datetime.fromisoformat(
            ALPHAVANTAGE_THROTTLE_FILE.read_text().strip())
    except Exception:
        return True
    elapsed_hours = (datetime.datetime.utcnow() - last).total_seconds() / 3600
    return elapsed_hours >= ALPHAVANTAGE_MIN_HOURS

def alphavantage_mark_run():
    OUTPUT_DIR.mkdir(exist_ok=True)
    ALPHAVANTAGE_THROTTLE_FILE.write_text(datetime.datetime.utcnow().isoformat())


async def fetch_alphavantage_multi(client: httpx.AsyncClient, feed: dict,
                                    area_id: str, area_label: str,
                                    protocol: dict) -> list[dict]:
    """Fetch S&P 500 (via SPY), oro (via GLD), EUR/USD da Alpha Vantage.
    Richiede API key gratuita in ALPHAVANTAGE_KEY. Fonte ridondante a
    Yahoo Finance — se manca la chiave o il throttling blocca il run,
    si auto-esclude senza generare errore falso."""
    import os

    api_key = os.environ.get("ALPHAVANTAGE_KEY", "").strip()
    if not api_key:
        return []  # nessuna chiave configurata — non è un errore, è opzionale

    if not alphavantage_should_run():
        return []  # throttling: rispettiamo il budget giornaliero gratuito

    events: list[dict] = []
    for sym in feed.get("symbols", []):
        function = sym["function"]
        params = {"function": function, "apikey": api_key}
        series_key = None
        if function == "TIME_SERIES_DAILY":
            params["symbol"] = sym["symbol"]
            series_key = "Time Series (Daily)"
        elif function == "FX_DAILY":
            params["from_symbol"] = sym["from_symbol"]
            params["to_symbol"] = sym["to_symbol"]
            series_key = "Time Series FX (Daily)"
        else:
            continue

        source_id = f"{feed['id']}_{sym['key']}"
        try:
            r = await client.get(feed["url"], params=params, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            series = data.get(series_key)
            if not series:
                record_error(source_id, area_id,
                             data.get("Error Message") or data.get("Note") or "risposta senza serie dati")
                continue
            dates_sorted = sorted(series.keys(), reverse=True)
            if len(dates_sorted) < 2:
                continue
            last_close = float(series[dates_sorted[0]]["4. close"])
            prev_close = float(series[dates_sorted[1]]["4. close"])
        except Exception as e:
            record_error(source_id, area_id, e)
            continue

        pct_change = ((last_close - prev_close) / prev_close * 100) if prev_close else 0.0
        threshold_key, name = sym["key"], sym["name"]
        th_def = protocol.get("thresholds", {}).get(threshold_key, {})
        measured = pct_change
        title = f"{name}: {last_close:.4f} ({pct_change:+.2f}% vs seduta prec.)"
        triggered, level = evaluate_threshold(measured, threshold_key, protocol)
        flags = fetcher_flags(threshold_key, round(measured, 3),
                               th_def.get("unit", ""), triggered, level,
                               notes=f"Ultima chiusura: {last_close:.4f} ({dates_sorted[0]}), "
                                     f"precedente: {prev_close:.4f} ({dates_sorted[1]}) (fonte: Alpha Vantage)")
        events.append({
            "event_id":       make_event_id(area_id, title, source_id),
            "schema_version": SCHEMA_VER,
            "extracted_at":   now_utc(),
            "meg_area":       area_id,
            "meg_area_label": area_label,
            "source":         {**source_block(feed), "id": source_id},
            "content": {
                "title":        title,
                "summary":      f"Ultime {len(dates_sorted)} sedute disponibili su Alpha Vantage",
                "original_url": "https://www.alphavantage.co/",
                "published_at": f"{dates_sorted[0]}T00:00:00Z",
                "language":     "en",
            },
            "meg_flags": flags,
        })

    if events:
        alphavantage_mark_run()
    return events


''' + marker1
        if marker1 in text:
            text = text.replace(marker1, insert, 1)
            changed = True
            print("  [OK] fetch_alphavantage_multi + throttling aggiunti")
        else:
            print("  [SKIP] anchor fetch_rss_feed non trovato")
    else:
        print("  [SKIP] fetch_alphavantage_multi già presente")

    marker2 = '    elif ftype == "yahoo_multi":\n        return await fetch_yahoo_finance_multi(client, feed, area_id, area_label, protocol)\n'
    marker2_new = marker2 + '    elif ftype == "alphavantage_multi":\n        return await fetch_alphavantage_multi(client, feed, area_id, area_label, protocol)\n'
    if 'ftype == "alphavantage_multi"' not in text:
        if marker2 in text:
            text = text.replace(marker2, marker2_new, 1)
            changed = True
            print("  [OK] dispatcher aggiornato con ramo alphavantage_multi")
        else:
            print("  [ATTENZIONE] anchor dispatcher yahoo_multi non trovato")
    else:
        print("  [SKIP] ramo alphavantage_multi già registrato")

    if changed:
        backup(FETCHER)
        FETCHER.write_text(text, encoding="utf-8")
        print("meg_fetcher.py aggiornato.")
    else:
        print("meg_fetcher.py: nessuna modifica necessaria.")


def patch_sources():
    text = SOURCES.read_text(encoding="utf-8")
    if "alphavantage_multi" in text:
        print("meg_sources.yaml: fonte Alpha Vantage già presente, nessuna modifica.")
        return

    anchor = '''    - id: yahoo_finance_multi
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
    addition = '''    - id: alphavantage_multi
      label: Alpha Vantage S&P/Oro/EUR-USD (richiede ALPHAVANTAGE_KEY, throttled 3h)
      url: https://www.alphavantage.co/query
      type: alphavantage_multi
      tier: 1
      symbols:
        - { function: "TIME_SERIES_DAILY", symbol: "SPY", key: "stock_index_drawdown", name: "S&P 500 (SPY ETF)" }
        - { function: "TIME_SERIES_DAILY", symbol: "GLD", key: "gold_price_surge",     name: "Oro (GLD ETF)" }
        - { function: "FX_DAILY", from_symbol: "EUR", to_symbol: "USD", key: "fx_volatility", name: "EUR/USD" }
'''
    if anchor in text:
        text = text.replace(anchor, anchor + addition, 1)
        backup(SOURCES)
        SOURCES.write_text(text, encoding="utf-8")
        print("meg_sources.yaml aggiornato: aggiunta fonte alphavantage_multi")
    else:
        print("  [ATTENZIONE] blocco yahoo_finance_multi non trovato esattamente come atteso.")
        print("  Aggiungere manualmente la fonte alphavantage_multi sotto B7.")


if __name__ == "__main__":
    print("Patch meg_fetcher.py (Alpha Vantage, fonte ridondante per B7)...")
    patch_fetcher()
    print()
    print("Patch meg_sources.yaml...")
    patch_sources()
    print()
    print("Verifica sintassi con:")
    print('  python3 -c "import ast; ast.parse(open(\'meg_fetcher.py\').read())"')
