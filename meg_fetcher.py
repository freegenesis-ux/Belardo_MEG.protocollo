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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

# Registro errori per source-health-check — popolato dai fetch_* che
# falliscono realmente (non gli "empty legittimi", es. nessuno storm attivo).
# Azzerato a inizio di ogni fetch_all() per evitare leak tra run nello stesso processo.
FETCH_ERRORS: list[dict] = []

def record_error(source_id: str, area_id: str, msg: str):
    FETCH_ERRORS.append({
        "source_id": source_id,
        "area_id":   area_id,
        "error":     str(msg)[:300],
        "at":        now_utc(),
    })
    print(f"  [WARN] fonte fallita ({source_id}, area {area_id}): {msg}")


# ReliefWeb applica rate-limit su richieste simultanee dallo stesso IP —
# questo semaforo serializza solo le chiamate verso reliefweb.int, senza
# rallentare la concorrenza delle altre fonti (che non hanno il problema).
RELIEFWEB_SEMAPHORE = asyncio.Semaphore(1)


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


def normalize_ingv_event(fields: list, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> Optional[dict]:
    """Parser per il formato pipe-delimited del webservice FDSNWS-event
    INGV: EventID|Time|Lat|Lon|Depth|Author|Catalog|Contributor|
    ContributorID|MagType|Magnitude|MagAuthor|EventLocationName|EventType"""
    if len(fields) < 13:
        return None
    try:
        event_id_raw = fields[0].strip()
        time_str     = fields[1].strip()
        depth        = fields[4].strip()
        mag_str      = fields[10].strip()
        location     = fields[12].strip()
    except IndexError:
        return None

    if not mag_str:
        return None
    try:
        mag = float(mag_str)
    except ValueError:
        return None

    # Normalizza timestamp: "2026-07-31T17:46:43.740000" -> "...Z" (troncato ai secondi)
    t_str = time_str.split(".")[0] + "Z" if time_str else None
    title = f"INGV M{mag} — {location}" if location else f"INGV M{mag}"
    depth_note = f"Profondità: {depth} km" if depth else ""

    triggered, level = evaluate_threshold(mag, "earthquake_magnitude", protocol)

    # Soglia locale abbassata per aree vulcaniche attive (bradisismo Campi
    # Flegrei / Vesuvio): un M3.0 lì è operativamente rilevante anche se
    # sotto la soglia globale pensata per sismicità generica mondiale.
    loc_lower = location.lower()
    is_volcanic_area = "flegrei" in loc_lower or "vesuv" in loc_lower or "pozzuoli" in loc_lower
    note_suffix = ""
    if is_volcanic_area and mag >= 3.0 and level in ("INATTIVO", "WATCHLIST"):
        triggered, level = True, "ATTIVO"
        note_suffix = " [soglia locale area vulcanica attiva — bradisismo]"

    return {
        "event_id":       make_event_id(area_id, title, feed["id"]),
        "schema_version": SCHEMA_VER,
        "extracted_at":   now_utc(),
        "meg_area":       area_id,
        "meg_area_label": area_label,
        "source":         source_block(feed),
        "content": {
            "title":        title,
            "summary":      f"Magnitudo {mag} | {location} | {depth_note}".strip(" |"),
            "original_url": f"https://terremoti.ingv.it/event/{event_id_raw}" if event_id_raw else None,
            "published_at": t_str,
            "language":     "it",
        },
        "meg_flags": fetcher_flags(
            threshold_key="earthquake_magnitude",
            value=mag,
            unit="Md",
            triggered=triggered,
            level=level,
            notes=(depth_note + note_suffix).strip(),
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


async def solve_stooq_challenge(client: httpx.AsyncClient, base_url: str) -> None:
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

    m = re.search(r'c="([^"]+)"\s*,\s*d=(\d+)', r.text)
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

    # Risolve la eventuale challenge PoW una sola volta per l'intero batch
    # di simboli — il cookie ottenuto resta valido nel client condiviso.
    await solve_stooq_challenge(client, feed["url"])

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
        except Exception as e:
            record_error(f"{feed.get('id','?')}_{symbol}", area_id, e)
            continue  # simbolo singolo non raggiungibile — non blocca gli altri
    return events


# ── FETCHERS ──────────────────────────────────────────────────────────────────

async def fetch_yahoo_finance_multi(client: httpx.AsyncClient, feed: dict,
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


ALPHAVANTAGE_THROTTLE_FILE = OUTPUT_DIR / ".alphavantage_last_run"
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
        await asyncio.sleep(13)  # rispetta il limite 5 richieste/minuto del piano free
        try:
            r = await client.get(feed["url"], params=params, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            series = data.get(series_key)
            if not series:
                record_error(source_id, area_id,
                             data.get("Error Message") or data.get("Note")
                             or data.get("Information") or "risposta senza serie dati")
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


async def fetch_copernicus_ems(client: httpx.AsyncClient, feed: dict,
                                area_id: str, area_label: str,
                                protocol: dict) -> list[dict]:
    """Fetch attivazioni Copernicus EMS dall'API JSON pubblica (l'RSS
    storico è stato dismesso). Dati più ricchi: paese, categoria,
    tempo di attivazione, stato aperto/chiuso."""
    events: list[dict] = []
    try:
        r = await client.get(feed["url"], timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        record_error(feed.get("id", "?"), area_id, e)
        return []

    for act in data.get("results", [])[:MAX_ITEMS]:
        code = act.get("code", "")
        name = act.get("name", "").strip()
        if not name:
            continue
        countries = ", ".join(act.get("countries", []))
        category = act.get("category", "")
        closed = act.get("closed", False)
        activation_time = act.get("activationTime", "")
        title = f"{code} — {name}"

        events.append({
            "event_id":       make_event_id(area_id, title, feed["id"]),
            "schema_version": SCHEMA_VER,
            "extracted_at":   now_utc(),
            "meg_area":       area_id,
            "meg_area_label": area_label,
            "source":         source_block(feed),
            "content": {
                "title":        title,
                "summary":      f"Categoria: {category} | Paesi: {countries} | Stato: {'chiusa' if closed else 'attiva'}",
                "original_url": f"https://mapping.emergency.copernicus.eu/activations/{code}/",
                "published_at": activation_time or None,
                "language":     "en",
            },
            "meg_flags": pending_flags("Attivazione Copernicus EMS — impatto da valutare"),
        })
    return events


async def fetch_rss_feed(client: httpx.AsyncClient, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> list[dict]:
    is_reliefweb = "reliefweb.int" in feed.get("url", "")
    try:
        if is_reliefweb:
            async with RELIEFWEB_SEMAPHORE:
                r = await fetch_with_retry(client, feed["url"])
                await asyncio.sleep(1.5)
                parsed = feedparser.parse(r.text)
                if len(parsed.entries) == 0:
                    # rate-limit condiviso sui runner GitHub (IP pool) — un retry
                    # dopo pausa più lunga spesso basta a superarlo
                    await asyncio.sleep(5)
                    r = await fetch_with_retry(client, feed["url"])
                    parsed = feedparser.parse(r.text)
        else:
            r = await fetch_with_retry(client, feed["url"])
            parsed = feedparser.parse(r.text)
        events = []
        for entry in parsed.entries[:MAX_ITEMS]:
            ev = normalize_rss_item(entry, feed, area_id, area_label)
            if ev:
                events.append(ev)
        return events
    except Exception as e:
        record_error(feed.get("id", "?"), area_id, e)
        return []


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
    except Exception as e:
        record_error(feed.get("id","?"), area_id, e)
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
    except Exception as e:
        record_error(feed.get("id","?"), area_id, e)
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
    except Exception as e:
        record_error(feed.get("id","?"), area_id, e)
        return []


async def fetch_ingv_events(client: httpx.AsyncClient, feed: dict,
                             area_id: str, area_label: str,
                             protocol: dict) -> list[dict]:
    """Fetch dal webservice FDSNWS-event INGV — formato testo pipe-delimited,
    finestra temporale dinamica (ultime 24h), soglia minmag configurabile
    nella fonte (default 1.5)."""
    start = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    minmag = feed.get("minmag", 1.5)
    url = (f"{feed['url']}?starttime={start}&minmag={minmag}"
           f"&format=text&orderby=time")
    events: list[dict] = []
    try:
        r = await fetch_with_retry(client, url, timeout=FETCH_TIMEOUT, attempts=2)
        lines = [ln for ln in r.text.splitlines() if ln.strip() and not ln.startswith("#")]
        for line in lines[:MAX_ITEMS * 3]:  # finestra INGV più densa di USGS
            fields = line.split("|")
            ev = normalize_ingv_event(fields, feed, area_id, area_label, protocol)
            if ev:
                events.append(ev)
    except Exception as e:
        print(f"  [WARN] fetch_ingv_events fallito ({feed.get('id')}): {e}")
    return events


async def fetch_one_feed(client: httpx.AsyncClient, feed: dict,
                          area_id: str, area_label: str,
                          protocol: dict) -> list[dict]:
    ftype = feed.get("type", "rss")
    url   = feed.get("url", "")

    if ftype == "rss":
        return await fetch_rss_feed(client, feed, area_id, area_label, protocol)
    elif ftype == "geojson":
        return await fetch_usgs(client, feed, area_id, area_label, protocol)
    elif ftype == "ingv_text":
        return await fetch_ingv_events(client, feed, area_id, area_label, protocol)
    elif ftype == "stooq_multi":
        return await fetch_stooq_multi(client, feed, area_id, area_label, protocol)
    elif ftype == "yahoo_multi":
        return await fetch_yahoo_finance_multi(client, feed, area_id, area_label, protocol)
    elif ftype == "alphavantage_multi":
        return await fetch_alphavantage_multi(client, feed, area_id, area_label, protocol)
    elif ftype == "copernicus_ems_json":
        return await fetch_copernicus_ems(client, feed, area_id, area_label, protocol)
    elif ftype == "json":
        if "swpc" in url:
            return await fetch_noaa_swpc(client, feed, area_id, area_label, protocol)
        elif "eonet" in url:
            return await fetch_nasa_eonet(client, feed, area_id, area_label, protocol)
    return []


# ── MAIN FETCH ORCHESTRATOR ───────────────────────────────────────────────────

def build_protocol_record(protocol: dict) -> dict:
    """Record di istruzioni auto-contenuto: qualunque AI o umano che apre
    il JSONL trova qui come interpretare meg_flags, le soglie numeriche, e
    come costruire un report — senza dipendere da documenti esterni."""
    macro_areas_summary = {
        area_id: {
            "name":      area_def["name"],
            "mandatory": area_def.get("mandatory", False),
            "note":      (area_def.get("note") or "").strip() or None,
        }
        for area_id, area_def in protocol.get("macro_areas", {}).items()
    }
    return {
        "record_type": "meg_protocol",
        "protocol_version": protocol.get("protocol", {}).get("version"),
        "generated_at": now_utc(),
        "field_semantics": {
            "filled_by":            "'fetcher' = valore numerico strutturato, valutato deterministicamente. 'pending_reasoning' = testo libero, richiede lettura/interpretazione da parte di un reasoning engine.",
            "quality_flag":         protocol.get("quality_flags", {}),
            "alert_level":          {
                "INATTIVO":    "Nessuna soglia superata.",
                "WATCHLIST":   "Sotto soglia di monitoraggio ma in avvicinamento (>=70% della soglia).",
                "ATTIVO":      "Soglia di monitoraggio superata — evento rilevante confermato.",
                "ALLERTA_MAX": "Soglia di allerta massima superata — priorità assoluta nel report.",
            },
            "threshold_triggered":  "true se alert_level è ATTIVO o ALLERTA_MAX.",
            "tier":                 "1 = fonte istituzionale/primaria obbligatoria. 2 = fonte secondaria, ammessa con segnalazione esplicita.",
        },
        "report_construction_rules": [
            "Includere sempre: ogni evento con alert_level ATTIVO o ALLERTA_MAX, indipendentemente dall'area.",
            "Includere: eventi pending_reasoning il cui contenuto testuale suggerisce escalation, anche senza soglia numerica — vanno letti e valutati, non scartati automaticamente.",
            "Scartare/riassumere in una riga sola: eventi INATTIVO privi di rilevanza contestuale (es. sismicità di fondo sotto soglia, comunicati istituzionali di routine).",
            "Collegare causalmente eventi correlati tra aree diverse quando plausibile (es. B1 geopolitica -> B6 energia -> B7 mercati) invece di elencarli come fatti isolati.",
            "Se un record source_status riporta status 'error' per una fonte tier 1, questo VA segnalato esplicitamente in apertura di report — non va ignorato silenziosamente.",
            "Finestra di riferimento raccomandata: rolling 48h dal timestamp extracted_at più recente nel file, salvo diversa richiesta esplicita.",
            "In caso di dati contrastanti tra fonti, segnalare il conflitto esplicitamente invece di produrne una media o sintesi che lo nasconda.",
        ],
        "thresholds": protocol.get("thresholds", {}),
        "macro_areas": macro_areas_summary,
        "tier1_sources": protocol.get("tier1_sources", []),
    }


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
    FETCH_ERRORS.clear()
    feed_lookup = {}  # (area_id, feed_id) -> feed dict (per source_status)

    async with httpx.AsyncClient() as client:
        tasks = []
        meta  = []
        for area_id, area_def in target.items():
            label = area_def["label"]
            for feed in area_def["feeds"]:
                tasks.append(
                    fetch_one_feed(client, feed, area_id, label, protocol))
                meta.append((area_id, feed["id"]))
                feed_lookup[(area_id, feed["id"])] = feed
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

    # Costruisce source_status per ogni fonte interrogata in questo run
    error_by_source = {e["source_id"]: e for e in FETCH_ERRORS}
    source_status = []
    for (area_id, feed_id), events in zip(meta, results):
        feed = feed_lookup.get((area_id, feed_id), {})
        err = error_by_source.get(feed_id)
        if err:
            status = "error"
        elif len(events) == 0:
            status = "empty"
        else:
            status = "ok"
        source_status.append({
            "id":              feed_id,
            "area":            area_id,
            "label":           feed.get("label"),
            "tier":            feed.get("tier", 2),
            "type":            feed.get("type"),
            "status":          status,
            "events_returned": len(events),
            "error":           err["error"] if err else None,
        })

    # Salva JSONL — record di metadata sempre in testa, poi gli eventi
    OUTPUT_DIR.mkdir(exist_ok=True)
    if output_path is None:
        output_path = OUTPUT_DIR / f"meg_events_{ts_file()}.jsonl"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(build_protocol_record(protocol), ensure_ascii=False) + "\n")
        f.write(json.dumps({
            "record_type": "source_status",
            "checked_at":  now_utc(),
            "sources":     source_status,
            "failed_count": sum(1 for s in source_status if s["status"] == "error"),
            "empty_count":  sum(1 for s in source_status if s["status"] == "empty"),
        }, ensure_ascii=False) + "\n")
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
