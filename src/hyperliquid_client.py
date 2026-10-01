"""Hyperliquid public order book client (fallback, no API key)."""
import requests

API = "https://api.hyperliquid.xyz/info"
MAP = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}

def _parse(entry) -> tuple:
    # Hyperliquid kirim dict {"px":..,"sz":..}, Binance kirim list [px, sz]
    if isinstance(entry, dict):
        return float(entry["px"]), float(entry["sz"])
    return float(entry[0]), float(entry[1])

def fetch_depth(symbol: str, limit: int = 100, timeout: int = 25) -> dict:
    coin = MAP.get(symbol, "BTC")
    r = requests.post(API, json={"type": "l2Book", "coin": coin}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    raw_levels = j.get("levels", [[], []])
    bids = [_parse(e) for e in raw_levels[0][:limit]]
    asks = [_parse(e) for e in raw_levels[1][:limit]]
    # Hyperliquid bids desc, asks asc already
    return {"bids": [[str(p), str(q)] for p, q in bids],
            "asks": [[str(p), str(q)] for p, q in asks]}
