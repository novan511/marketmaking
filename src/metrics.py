"""Pure metric functions — sesuaikan spec-mvp.md."""

def mid_price(best_bid: float, best_ask: float) -> float:
    return (best_bid + best_ask) / 2

def spread_bps(best_bid: float, best_ask: float) -> float:
    mid = mid_price(best_bid, best_ask)
    return (best_ask - best_bid) / mid * 10000

def depth_2pct(bids: list, asks: list, mid: float) -> tuple:
    bid_floor = mid * 0.98
    ask_cap = mid * 1.02
    bid_usd = sum(p * q for p, q in bids if p >= bid_floor)
    ask_usd = sum(p * q for p, q in asks if p <= ask_cap)
    return bid_usd, ask_usd

def imbalance(bids: list, asks: list, mid: float, pct: float = 0.01) -> float:
    bb = sum(q for p, q in bids if p >= mid * (1 - pct))
    ba = sum(q for p, q in asks if p <= mid * (1 + pct))
    denom = bb + ba
    return (bb - ba) / denom if denom else 0.0
