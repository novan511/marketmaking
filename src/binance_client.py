"""Binance public order book client (REST MVP)."""
import time
import requests

# api.binance.com diblokir di beberapa ISP ID -> pakai vision (public market data only)
BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]

def fetch_depth(symbol: str, limit: int = 100, timeout: int = 25) -> dict:
    last_err = None
    for base in BASES:
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{base}/api/v3/depth",
                    params={"symbol": symbol, "limit": limit},
                    timeout=timeout,
                )
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                time.sleep(1 + attempt)  # backoff 1s, 2s
    raise RuntimeError(f"Semua endpoint sibuk/timeout. Terakhir: {last_err}")
