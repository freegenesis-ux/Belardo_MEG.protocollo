#!/usr/bin/env python3
"""Diagnostica isolata: replica passo-passo solve_stooq_challenge con
output verboso ad ogni fase, per capire esattamente dove si interrompe."""

import asyncio
import hashlib
import re
import httpx

BASE_URL = "https://stooq.com/q/d/l/"
TEST_URL = "https://stooq.com/q/d/l/?s=%5Espx&d1=20260718&d2=20260801&i=d"

async def main():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://stooq.com/q/d/?s=spx",
    }
    async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client:
        print("--- STEP 1: GET iniziale ---")
        r = await client.get(TEST_URL, timeout=14)
        print("status:", r.status_code)
        print("primi 200 char:", r.text[:200])
        print()

        if "requires JavaScript to verify" not in r.text:
            print(">>> Nessuna challenge presente, risposta diretta valida.")
            return

        print("--- STEP 2: estrazione c e d ---")
        m = re.search(r'c="([^"]+)"\s*,\s*d=(\d+)', r.text)
        if not m:
            print(">>> REGEX NON HA MATCHATO. Testo completo per ispezione:")
            print(r.text[:1000])
            return
        c, d = m.group(1), int(m.group(2))
        print(f"c estratto: {c}")
        print(f"d estratto: {d}")
        print()

        print("--- STEP 3: mining nonce ---")
        target = "0" * d
        n = 0
        while True:
            h = hashlib.sha256(f"{c}{n}".encode()).hexdigest()
            if h.startswith(target):
                break
            n += 1
            if n > 2_000_000:
                print(">>> Nonce non trovato entro 2M iterazioni, qualcosa non torna.")
                return
        print(f"nonce trovato: n={n}, hash={h}")
        print()

        print("--- STEP 4: POST /__verify ---")
        origin = "https://stooq.com"
        r2 = await client.post(f"{origin}/__verify",
                                data={"c": c, "n": str(n)},
                                timeout=14)
        print("status POST:", r2.status_code)
        print("headers risposta:", dict(r2.headers))
        print("cookies nel client dopo POST:", dict(client.cookies))
        print("body risposta (primi 300 char):", r2.text[:300])
        print()

        print("--- STEP 5: GET finale (dovrebbe restituire il CSV vero) ---")
        r3 = await client.get(TEST_URL, timeout=14)
        print("status:", r3.status_code)
        print("primi 300 char:", r3.text[:300])

if __name__ == "__main__":
    asyncio.run(main())
