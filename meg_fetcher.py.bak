#!/usr/bin/env python3
"""
MEG FETCHER v1.0
=================
Raccoglie le fonti configurate in meg_sources.yaml, normalizza ogni
notizia nello schema MEG Event v1.0, e salva un file JSONL timestampato.

Logica flags:
  - Fonti strutturate (USGS GeoJSON, NOAA SWPC JSON):
    filled_by = "fetcher"  — valore numerico estratto, soglia confrontata
    deterministicamente contro meg_protocol.yaml
  - Fonti testuali (RSS):
    filled_by = "pending_reasoning" — nessuna interpretazione del testo
    nel fetcher. Il reasoning engine leggerà il sommario e valuterà.

Output: meg_events/meg_events_YYYYMMDD_HHMM.jsonl
Ogni riga è un MEG Event JSON indipendente.

Uso standalone:
    python3 meg_fetcher.py
    python3 meg_fetcher.py --area B1
    python3 meg_fetcher.py --out /path/to/output.jsonl
"""

import asyncio
import hashlib
import json
import argparse
import datetime
from pathlib import Path
from typing import Optional

import httpx
import feedparser
import yaml

BASE          = Path(__file__).parent
SOURCES_PATH  = BASE / "meg_sources.yaml"
PROTOCOL_PATH = BASE / "meg_protocol.yaml"
OUTPUT_DIR    = BASE / "meg_events"
FETCH_TIMEOUT = 14
MAX_ITEMS     = 10
SCHEMA_VER    = "1.0"

# Rotazione User-Agent + retry — stessa logica di resilienza del Pannello CV
# (dove un singolo proxy CORS instabile veniva scavalcato con una catena di
# fallback). Qui non serve un proxy (il fetcher gira server-side, nessun CORS),
# ma molte fonti istituzionali bloccano UA "python-requests" di default:
# ruotare UA + ritentare 1 volta alza sensibilmente il tasso di successo.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MEGFetcher/3.0",
    "MEGFetcher/3.0 (+monitoraggio eventi globali; contatto operatore)",
]

async def fetch_with_retry(client: httpx.AsyncClient, url: str,
                            timeout: float = FETCH_TIMEOUT, attempts: int = 2):
    """GET con retry e rotazione User-Agent. Solleva l'ultima eccezione se
    tutti i tentativi falliscono — il chiamante decide come gestirla."""
    last_exc = None
    for i in range(attempts):
        try:
            r = await client.get(
                url, timeout=timeout, follow_redirects=True,
                headers={"User-Agent": USER_AGENTS[i % len(USER_AGENTS)]})
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            await asyncio.sleep(0.4 * (i + 1))
    raise last_exc


# ── LOADERS ───────────────────────────────────────────────────────────────────

def load_sources() -> dict:
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]

def load_protocol() -> dict:
    if not PROTOCOL_PATH.exists():
        return {}
    with open(PROTOCOL_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def now_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def ts_file() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")


# ── EVENT ID ─────────────────────────────────────────────────────────────────

def make_event_id(area: str, title: str, source_id: str) -> str:
    """
    ID deterministico: stesso evento dalla stessa fonte nello stesso giorno
    produce sempre lo stesso ID — evita duplicati in sessioni ravvicinate.
    """
    date_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    raw = f"{area}|{source_id}|{title}|{date_str}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"meg-{area.lower()}-{date_str}-{h}"


# ── THRESHOLD EVALUATION (deterministic, fetcher-side only) ──────────────────

def evaluate_threshold(value: float, threshold_key: str,
                        protocol: dict) -> tuple[bool, str]:
    """
    Confronta un valore numerico con le soglie del protocollo.
    Restituisce (triggered: bool, alert_level: str).
    Usata solo per dati strutturati (USGS, NOAA) — mai su testo libero.
    """
    thresholds = protocol.get("thresholds", {})
    th = thresholds.get(threshold_key)
    if not th:
        return False, "INATTIVO"

    direction  = th.get("direction", "above")
    monitor    = th.get("monitor")
    max_alert  = th.get("max_alert")

    if monitor is None:
        return False, "INATTIVO"

    triggered = False
    level     = "INATTIVO"

    if direction == "above":
        if max_alert is not None and value >= max_alert:
            triggered, level = True, "ALLERTA_MAX"
        elif value >= monitor:
            triggered, level = True, "ATTIVO"
        elif value >= monitor * 0.7:
            triggered, level = False, "WATCHLIST"
    elif direction == "below":
        if max_alert is not None and value <= max_alert:
            triggered, level = True, "ALLERTA_MAX"
        elif value <= monitor:
            triggered, level = True, "ATTIVO"
        elif value <= monitor * 1.3:
            triggered, level = False, "WATCHLIST"
    elif direction == "absolute":
        av = abs(value)
        if max_alert is not None and av >= max_alert:
            triggered, level = True, "ALLERTA_MAX"
        elif av >= monitor:
            triggered, level = True, "ATTIVO"
        elif av >= monitor * 0.7:
            triggered, level = False, "WATCHLIST"

    return triggered, level


# ── EMPTY FLAGS (pending) ─────────────────────────────────────────────────────

def pending_flags(notes: str = "") -> dict:
    return {
        "filled_by":           "pending_reasoning",
        "threshold_key":       None,
        "measured_value":      None,
        "unit":                None,
        "threshold_triggered": None,
        "alert_level":         None,
        "quality_flag":        "IN_VERIFICA",
        "notes":               notes or None,
    }

def fetcher_flags(threshold_key: str, value: float, unit: str,
                   triggered: bool, level: str, notes: str = "") -> dict:
    return {
        "filled_by":           "fetcher",
        "threshold_key":       threshold_key,
        "measured_value":      value,
        "unit":                unit,
        "threshold_triggered": triggered,
        "alert_level":         level,
        "quality_flag":        "VERIFICATO",
        "notes":               notes or None,
    }


# ── SOURCE BLOCK ──────────────────────────────────────────────────────────────

def source_block(feed: dict) -> dict:
    return {
        "id":    feed["id"],
        "label": feed["label"],
        "url":   feed["url"],
        "tier":  feed.get("tier", 2),
        "type":  feed.get("type", "rss"),
    }


# ── NORMALIZERS ───────────────────────────────────────────────────────────────

def normalize_rss_item(entry, feed: dict, area_id: str,
                        area_label: str) -> Optional[dict]:
    title   = getattr(entry, "title",   "").strip()
    link    = getattr(entry, "link",    "").strip()
    pub     = getattr(entry, "published", "") or getattr(entry, "updated", "")
    summary = getattr(entry, "summary", "").strip()

    if not title:
        return None

    # Detect language hint from feed URL (heuristic, non critico)
    url = feed.get("url", "")
    lang = "it" if any(x in url for x in [".it", "/it/", "ansa", "campania"]) else "en"

    return {
        "event_id":       make_event_id(area_id, title, feed["id"]),
        "schema_version": SCHEMA_VER,
        "extracted_at":   now_utc(),
        "meg_area":       area_id,
        "meg_area_label": area_label,
        "source":         source_block(feed),
        "content": {
            "title":        title,
            "summary":      summary[:500] if summary else None,
            "original_url": link or None,
            "published_at": pub or None,
            "language":     lang,
        },
        "meg_flags": pending_flags(),
    }


def normalize_usgs_feature(feature: dict, feed: dict,
                             area_id: str, area_label: str,
                             protocol: dict) -> Optional[dict]:
    props = feature.get("properties", {})
    mag   = props.get("mag")
    place = props.get("place", "Unknown location")
    t_ms  = props.get("time", 0)
    url   = props.get("url", "")
    depth = feature.get("geometry", {}).get("coordinates", [None, None, None])[2]

    if mag is None:
        return None

    t_str = (datetime.datetime.utcfromtimestamp(t_ms / 1000)
             .strftime("%Y-%m-%dT%H:%M:%SZ") if t_ms else None)
    title = f"M{mag} — {place}"

    triggered, level = evaluate_threshold(mag, "earthquake_magnitude", protocol)
    depth_note = f"Profondità: {depth} km" if depth is not None else ""

    return {
        "event_id":       make_event_id(area_id, title, feed["id"]),
        "schema_version": SCHEMA_VER,
        "extracted_at":   now_utc(),
        "meg_area":       area_id,
        "meg_area_label": area_label,
        "source":         source_block(feed),
        "content": {
            "title":        title,
            "summary":      f"Magnitudo {mag} | {place} | {depth_note}".strip(" |"),
            "original_url": url or None,
            "published_at": t_str,
            "language":     "en",
        },
        "meg_flags": fetcher_flags(
            threshold_key="earthquake_magnitude",
            value=float(mag),
            unit="Mw",
            triggered=triggered,
            level=level,
            notes=depth_note,
        ),
    }


def normalize_noaa_alert(alert: dict, feed: dict,
                           area_id: str, area_label: str,
                           protocol: dict) -> Optional[dict]:
    msg   = alert.get("message", "")
    date  = alert.get("issue_datetime", "")
    title = msg.split("\n")[0][:120].strip() if msg else "NOAA SWPC Alert"

    # Estrai livello G dalla stringa (es. "G3", "G4", "G5")
    import re
    g_match = re.search(r"G(\d)", msg)
    g_level = int(g_match.group(1)) if g_match else None

    if g_level is not None:
        triggered, level = evaluate_threshold(
            g_level, "geomagnetic_storm", protocol)
        flags = fetcher_flags("geomagnetic_storm", float(g_level), "G-scale",
                               triggered, level)
    else:
        flags = pending_flags("Livello G non rilevabile dal testo — passa a reasoning")

    return {
        "event_id":       make_event_id(area_id, title, feed["id"]),
        "schema_version": SCHEMA_VER,
        "extracted_at":   now_utc(),
        "meg_area":       area_id,
        "meg_area_label": area_label,
        "source":         source_block(feed),
        "content": {
            "title":        title,
            "summary":      msg[:500] if msg else None,
            "original_url": feed["url"],
            "published_at": date or None,
            "language":     "en",
        },
        "meg_flags": flags,
    }


def normalize_nasa_eonet(evt: dict, feed: dict,
                          area_id: str, area_label: str) -> Optional[dict]:
    title = evt.get("title", "").strip()
    if not title:
        return None
    cats  = ", ".join(c["title"] for c in evt.get("categories", []))
    geoms = evt.get("geometry", [])
    date  = geoms[0].get("date", "") if geoms else ""
    link  = evt.get("sources", [{}])[0].get("url", "") if evt.get("sources") else ""

    return {
        "event_id":       make_event_id(area_id, title, feed["id"]),
        "schema_version": SCHEMA_VER,
        "extracted_at":   now_utc(),
        "meg_area":       area_id,
        "meg_area_label": area_label,
        "source":         source_block(feed),
        "content": {
            "title":        title,
            "summary":      f"Categoria: {cats}" if cats else None,
            "original_url": link or None,
            "published_at": date or None,
            "language":     "en",
        },
        "meg_flags": pending_flags("Evento EONET — tipologia e impatto da valutare"),
    }


# ── STOOQ (mercati/valute/oro — dati strutturati no-key) ─────────────────────

# Mappa simbolo -> (threshold_key, nome leggibile, "level" usa il valore
# assoluto dell'ultima chiusura invece della variazione %)
STOOQ_THRESHOLD_MAP = {
    "^spx":   ("stock_index_drawdown", "S&P 500", "pct"),
    "^dax":   ("stock_index_drawdown", "DAX", "pct"),
    "^vix":   ("vix_level",            "VIX", "level"),
    "xauusd": ("gold_price_surge",     "Oro Spot (XAU/USD)", "pct"),
    "eurusd": ("fx_volatility",        "EUR/USD", "pct"),
}

def normalize_stooq_symbol(symbol: str, rows: list, feed: dict,
                            area_id: str, area_label: str,
                            protocol: dict) -> Optional[dict]:
    """rows: lista di dict CSV con chiavi Date,Open,High,Low,Close (ultime
    N sedute, ordine cronologico crescente). Calcola la variazione % tra le
    ultime due chiusure e valuta la soglia corrispondente."""
    valid = [r for r in rows if r.get("Close") not in (None, "", "N/D")]
    if len(valid) < 2:
        return None

    last, prev = valid[-1], valid[-2]
    try:
        last_close = float(last["Close"])
        prev_close = float(prev["Close"])
    except (TypeError, ValueError):
        return None

    pct_change = ((last_close - prev_close) / prev_close * 100) if prev_close else 0.0
    threshold_key, name, mode = STOOQ_THRESHOLD_MAP.get(
        symbol, (None, symbol, "pct"))

    measured = last_close if mode == "level" else pct_change
    title = (f"{name}: {last_close:.2f} ({pct_change:+.2f}% vs seduta prec.)"
             if mode != "level" else
             f"{name}: {last_close:.2f} (var. giornaliera {pct_change:+.2f}%)")

    if threshold_key:
        triggered, level = evaluate_threshold(measured, threshold_key, protocol)
        flags = fetcher_flags(threshold_key, round(measured, 3),
                               protocol["thresholds"][threshold_key]["unit"],
                               triggered, level,
                               notes=f"Chiusura {last['Date']}: {last_close:.2f}, "
                                     f"seduta prec. {prev['Date']}: {prev_close:.2f}")
    else:
        flags = pending_flags(f"Simbolo {symbol} senza soglia mappata")

    return {
        "event_id":       make_event_id(area_id, title, f"{feed['id']}_{symbol}"),
        "schema_version": SCHEMA_VER,
        "extracted_at":   now_utc(),
        "meg_area":       area_id,
        "meg_area_label": area_label,
        "source":         {**source_block(feed), "id": f"{feed['id']}_{symbol}"},
        "content": {
            "title":        title,
            "summary":      f"Ultime {len(valid)} sedute disponibili su Stooq",
            "original_url": f"https://stooq.com/q/?s={symbol}",
            "published_at": f"{last['Date']}T00:00:00Z",
            "language":     "en",
        },
        "meg_flags": flags,
    }


async def fetch_stooq_multi(client: httpx.AsyncClient, feed: dict,
                             area_id: str, area_label: str,
                             protocol: dict) -> list[dict]:
    """Fetcha lo storico breve (10gg) di ogni simbolo configurato via CSV
    Stooq (gratuito, no API key). Un simbolo che fallisce non blocca gli
    altri — stesso principio del panel radar-fallback: degradazione parziale,
    mai blocco totale."""
    import csv
    import io

    symbols = feed.get("symbols", [])
    events: list[dict] = []
    d2 = datetime.datetime.utcnow()
    d1 = d2 - datetime.timedelta(days=14)
    d1s, d2s = d1.strftime("%Y%m%d"), d2.strftime("%Y%m%d")

    for symbol in symbols:
        url = f"{feed['url']}?s={symbol}&d1={d1s}&d2={d2s}&i=d"
        try:
            r = await fetch_with_retry(client, url, timeout=10, attempts=2)
            reader = csv.DictReader(io.StringIO(r.text))
            rows = list(reader)
            ev = normalize_stooq_symbol(symbol, rows, feed, area_id,
                                          area_label, protocol)
            if ev:
                events.append(ev)
        except Exception:
            continue  # simbolo singolo non raggiungibile — non blocca gli altri
    return events


# ── FETCHERS ──────────────────────────────────────────────────────────────────

async def fetch_rss_feed(client: httpx.AsyncClient, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> list[dict]:
    try:
        r = await fetch_with_retry(client, feed["url"])
        parsed = feedparser.parse(r.text)
        events = []
        for entry in parsed.entries[:MAX_ITEMS]:
            ev = normalize_rss_item(entry, feed, area_id, area_label)
            if ev:
                events.append(ev)
        return events
    except Exception as e:
        return []  # Silenzioso — il server logga altrove


async def fetch_usgs(client: httpx.AsyncClient, feed: dict,
                      area_id: str, area_label: str,
                      protocol: dict) -> list[dict]:
    try:
        r = await client.get(feed["url"], timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        events = []
        for feat in data.get("features", [])[:MAX_ITEMS]:
            ev = normalize_usgs_feature(feat, feed, area_id, area_label, protocol)
            if ev:
                events.append(ev)
        return events
    except Exception:
        return []


async def fetch_noaa_swpc(client: httpx.AsyncClient, feed: dict,
                           area_id: str, area_label: str,
                           protocol: dict) -> list[dict]:
    try:
        r = await client.get(feed["url"], timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        events = []
        for alert in data[:MAX_ITEMS]:
            ev = normalize_noaa_alert(alert, feed, area_id, area_label, protocol)
            if ev:
                events.append(ev)
        return events
    except Exception:
        return []


async def fetch_nasa_eonet(client: httpx.AsyncClient, feed: dict,
                            area_id: str, area_label: str,
                            protocol: dict) -> list[dict]:
    try:
        r = await client.get(feed["url"], timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        events = []
        for evt in data.get("events", [])[:MAX_ITEMS]:
            ev = normalize_nasa_eonet(evt, feed, area_id, area_label)
            if ev:
                events.append(ev)
        return events
    except Exception:
        return []


async def fetch_one_feed(client: httpx.AsyncClient, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> list[dict]:
    ftype = feed.get("type", "rss")
    url   = feed.get("url", "")

    if ftype == "rss":
        return await fetch_rss_feed(client, feed, area_id, area_label, protocol)
    elif ftype == "geojson":
        return await fetch_usgs(client, feed, area_id, area_label, protocol)
    elif ftype == "stooq_multi":
        return await fetch_stooq_multi(client, feed, area_id, area_label, protocol)
    elif ftype == "json":
        if "swpc" in url:
            return await fetch_noaa_swpc(client, feed, area_id, area_label, protocol)
        elif "eonet" in url:
            return await fetch_nasa_eonet(client, feed, area_id, area_label, protocol)
    return []


# ── MAIN FETCH ORCHESTRATOR ───────────────────────────────────────────────────

async def fetch_all(area_filter: Optional[str] = None,
                     output_path: Optional[Path] = None) -> dict:
    """
    Fetcha tutte le fonti in parallelo.
    Restituisce { "events": [...], "stats": {...}, "output_file": str }
    """
    sources  = load_sources()
    protocol = load_protocol()

    # Filtra per area se richiesto
    target = ({area_filter: sources[area_filter]}
              if area_filter and area_filter in sources else sources)

    # Costruisce task paralleli
    async with httpx.AsyncClient() as client:
        tasks = []
        meta  = []
        for area_id, area_def in target.items():
            label = area_def["label"]
            for feed in area_def["feeds"]:
                tasks.append(
                    fetch_one_feed(client, feed, area_id, label, protocol))
                meta.append((area_id, feed["id"]))
        results = await asyncio.gather(*tasks)

    # Appiattisce in lista eventi unica
    all_events: list[dict] = []
    stats = {"total": 0, "by_area": {}, "triggered": 0, "pending": 0}

    for (area_id, feed_id), events in zip(meta, results):
        for ev in events:
            # Deduplication: salta event_id già visti in questa sessione
            if any(e["event_id"] == ev["event_id"] for e in all_events):
                continue
            all_events.append(ev)
            stats["total"] += 1
            stats["by_area"].setdefault(area_id, 0)
            stats["by_area"][area_id] += 1
            flags = ev.get("meg_flags", {})
            if flags.get("threshold_triggered"):
                stats["triggered"] += 1
            if flags.get("filled_by") == "pending_reasoning":
                stats["pending"] += 1

    # Salva JSONL
    OUTPUT_DIR.mkdir(exist_ok=True)
    if output_path is None:
        output_path = OUTPUT_DIR / f"meg_events_{ts_file()}.jsonl"

    with open(output_path, "w", encoding="utf-8") as f:
        for ev in all_events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    stats["output_file"] = str(output_path)
    stats["fetched_at"]  = now_utc()
    return {"events": all_events, "stats": stats,
            "output_file": str(output_path)}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MEG Fetcher v1.0")
    parser.add_argument("--area", help="Filtra per macro-area (es. B1)")
    parser.add_argument("--out",  help="Percorso file JSONL di output")
    args = parser.parse_args()

    out = Path(args.out) if args.out else None
    result = asyncio.run(fetch_all(area_filter=args.area, output_path=out))
    s = result["stats"]
    print(f"MEG Fetcher completato: {s['total']} eventi raccolti")
    print(f"  Trigger attivi: {s['triggered']}")
    print(f"  In attesa reasoning: {s['pending']}")
    print(f"  File: {s['output_file']}")
    for area, count in s["by_area"].items():
        print(f"  {area}: {count} eventi")
