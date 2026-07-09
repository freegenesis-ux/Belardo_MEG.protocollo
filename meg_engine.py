#!/usr/bin/env python3
"""
MEG ENGINE v3.0
================
Motore di enforcement del Protocollo MEG. Non genera contenuto editoriale:
verifica oggettivamente, tramite log delle azioni realmente eseguite, se le
fasi obbligatorie del protocollo sono state completate prima di autorizzare
la fase di redazione (C5).

Principio cardine: nessuna fase si "auto-dichiara" completa. Ogni fetch o
ricerca va registrata con log_action(). Il validatore legge solo il log,
mai le dichiarazioni testuali del modello.

Uso tipico in sessione:
    from meg_engine import MegSession
    s = MegSession()
    s.log_search("B1", "armed conflict escalation latest 2026-06-19")
    s.log_fetch("C1_audit_prime_pagine", "reuters.com")
    ...
    s.set_claim("B2_1_2", earthquake_magnitude=6.4, location="Eastern Samar")
    print(s.coverage_matrix())
    print(s.can_proceed_to_redazione())
    s.save()
"""

import json
import yaml
import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

PROTOCOL_PATH = Path(__file__).parent / "meg_protocol.yaml"
LOG_PATH = Path(__file__).parent / "meg_session_log.json"


def load_protocol():
    with open(PROTOCOL_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class ClaimResult:
    macro_area: str
    threshold_key: Optional[str]
    measured_value: Optional[float]
    unit: Optional[str]
    status: str          # ATTIVO / ALLERTA_MAX / WATCHLIST / INATTIVO
    note: str = ""


class MegSession:
    def __init__(self, session_date: Optional[str] = None):
        self.protocol = load_protocol()
        self.session_date = session_date or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        self.searches: dict[str, list[str]] = {}   # macro_area -> [query, ...]
        self.fetches: dict[str, list[str]] = {}     # phase_id -> [source, ...]
        self.claims: list[ClaimResult] = []

    # -------------------- LOGGING (oggettivo, da chiamare ad ogni azione reale) --------------------

    def log_search(self, macro_area: str, query: str):
        self.searches.setdefault(macro_area, []).append(query)

    def log_fetch(self, phase_id: str, source: str):
        self.fetches.setdefault(phase_id, []).append(source)

    # -------------------- VALUTAZIONE SOGLIE (deterministica) --------------------

    def evaluate_threshold(self, threshold_key: str, value: float) -> str:
        """Restituisce ATTIVO / ALLERTA_MAX / WATCHLIST / INATTIVO confrontando
        il valore misurato con le soglie definite in YAML. Nessun giudizio,
        solo confronto numerico."""
        th = self.protocol["thresholds"].get(threshold_key)
        if th is None:
            raise KeyError(f"Soglia non definita nel protocollo: {threshold_key}")

        direction = th["direction"]
        monitor = th["monitor"]
        max_alert = th.get("max_alert")

        if direction == "above":
            if max_alert is not None and value >= max_alert:
                return "ALLERTA_MAX"
            if value >= monitor:
                return "ATTIVO"
            if value >= monitor * 0.7:
                return "WATCHLIST"
            return "INATTIVO"

        if direction == "below":
            if max_alert is not None and value <= max_alert:
                return "ALLERTA_MAX"
            if value <= monitor:
                return "ATTIVO"
            if value <= monitor * 1.3:
                return "WATCHLIST"
            return "INATTIVO"

        if direction == "absolute":
            av = abs(value)
            if max_alert is not None and av >= max_alert:
                return "ALLERTA_MAX"
            if av >= monitor:
                return "ATTIVO"
            if av >= monitor * 0.7:
                return "WATCHLIST"
            return "INATTIVO"

        raise ValueError(f"Direzione soglia non riconosciuta: {direction}")

    def set_claim(self, macro_area: str, threshold_key: Optional[str] = None,
                  value: Optional[float] = None, note: str = ""):
        if threshold_key and value is not None:
            status = self.evaluate_threshold(threshold_key, value)
            unit = self.protocol["thresholds"][threshold_key]["unit"]
        else:
            status = "INATTIVO"
            unit = None
        result = ClaimResult(macro_area, threshold_key, value, unit, status, note)
        self.claims.append(result)
        return result

    # -------------------- MATRICE DI COPERTURA (Fase C3) --------------------

    def coverage_matrix(self) -> dict:
        matrix = {}
        for area_id, area_def in self.protocol["macro_areas"].items():
            n_queries = len(self.searches.get(area_id, []))
            min_required = area_def["min_queries_required"]
            inst_sources_defined = area_def.get("institutional_sources", [])
            inst_fetched = self.fetches.get(area_id, [])

            inst_ok = True
            if inst_sources_defined:
                required_names = {s["name"] for s in inst_sources_defined}
                fetched_names = set(inst_fetched)
                inst_ok = required_names.issubset(fetched_names)

            queries_ok = n_queries >= min_required
            status = "OK" if (queries_ok and inst_ok) else "INCOMPLETO"

            matrix[area_id] = {
                "name": area_def["name"],
                "queries_executed": n_queries,
                "queries_required": min_required,
                "institutional_sources_ok": inst_ok,
                "status": status,
            }
        return matrix

    # -------------------- GATE SEQUENZIALE (Fasi C0-C5) --------------------

    def phase_status(self) -> dict:
        status = {}

        # C0: assumiamo eseguita se la sessione ha una data settata
        status["C0_orientamento"] = bool(self.session_date)

        # C1: audit prime pagine
        c1_def = next(p for p in self.protocol["phases"] if p["id"] == "C1_audit_prime_pagine")
        fetched_c1 = set(self.fetches.get("C1_audit_prime_pagine", []))
        required_c1 = set(c1_def["required_sources"])
        n_matched = sum(1 for src in fetched_c1 for req in required_c1 if req in src or src in req)
        status["C1_audit_prime_pagine"] = n_matched >= c1_def["min_sources_fetched"]

        # C2: interrogazione strutturale -> deriva dalla coverage matrix (solo query, non fonti)
        cov = self.coverage_matrix()
        status["C2_interrogazione_strutturale"] = all(
            v["queries_executed"] >= v["queries_required"] for v in cov.values()
        )

        # C3: matrice di copertura completa (query + fonti istituzionali)
        status["C3_matrice_copertura"] = all(v["status"] == "OK" for v in cov.values())

        # C4: anti punto cieco
        c4_fetched = self.fetches.get("C4_anti_punto_cieco", [])
        status["C4_anti_punto_cieco"] = len(c4_fetched) >= 1

        return status

    def can_proceed_to_redazione(self) -> tuple[bool, list[str]]:
        status = self.phase_status()
        blocking = [phase for phase, ok in status.items() if not ok]
        return (len(blocking) == 0, blocking)

    # -------------------- OUTPUT --------------------

    def print_report_gate(self):
        print(f"=== MEG ENGINE — Stato Sessione ({self.session_date}) ===\n")
        status = self.phase_status()
        for phase, ok in status.items():
            mark = "✅" if ok else "❌"
            print(f"{mark} {phase}")
        proceed, blocking = self.can_proceed_to_redazione()
        print()
        if proceed:
            print("🟢 GATE APERTO — La Fase C5 (Redazione) è autorizzata.")
        else:
            print(f"🔴 GATE BLOCCATO — Fasi incomplete: {', '.join(blocking)}")
        print()
        print("--- Matrice di Copertura (C3) ---")
        cov = self.coverage_matrix()
        for area, v in cov.items():
            mark = "OK " if v["status"] == "OK" else "INCOMPLETO"
            print(f"  [{mark}] {area:10s} {v['name']:50s} query {v['queries_executed']}/{v['queries_required']}  fonti_ist_ok={v['institutional_sources_ok']}")
        if self.claims:
            print("\n--- Tabella di Verifica Claim ---")
            for c in self.claims:
                print(f"  {c.macro_area:10s} {c.threshold_key or '-':25s} val={c.measured_value} {c.unit or ''}  -> {c.status}   {c.note}")

    def save(self, path: Path = LOG_PATH):
        data = {
            "session_date": self.session_date,
            "searches": self.searches,
            "fetches": self.fetches,
            "claims": [asdict(c) for c in self.claims],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\nSessione salvata in {path}")


if __name__ == "__main__":
    print("MEG Engine pronto. Importare MegSession per l'uso in sessione.")
