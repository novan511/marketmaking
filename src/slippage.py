"""Walk-the-book slippage calculator."""

def walk_book(levels: list, size: float) -> tuple:
    """levels: [(price, qty)] sorted best-first. Returns (avg_px, filled_qty)."""
    remaining = size
    notional = 0.0
    filled = 0.0
    for px, qty in levels:
        take = min(remaining, qty)
        notional += take * px
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    avg_px = notional / filled if filled else 0.0
    return avg_px, filled

def slippage_for_sell(bids: list, mid: float, size: float) -> dict:
    avg_px, filled = walk_book(bids, size)
    return {
        "avg_px": avg_px,
        "filled": filled,
        "filled_pct": filled / size * 100 if size else 0,
        "slippage_bps": (mid - avg_px) / mid * 10000 if mid and avg_px else 0,
    }

def slippage_for_buy(asks: list, mid: float, size: float) -> dict:
    avg_px, filled = walk_book(asks, size)
    return {
        "avg_px": avg_px,
        "filled": filled,
        "filled_pct": filled / size * 100 if size else 0,
        "slippage_bps": (avg_px - mid) / mid * 10000 if mid and avg_px else 0,
    }
