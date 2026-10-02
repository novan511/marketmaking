"""Quick sanity tests (tanpa pytest): python3 tests/test_events.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collector import diff_book
from src.events import EventDetector, Sample
from src.slippage import slippage_for_sell


def mk(ts, imb=0.0, spr=2.0, db=1_000_000.0, da=1_000_000.0, sym="BTCUSDT", **kw):
    return Sample(ts=ts, symbol=sym, mid=100_000.0, spread_bps=spr,
                  depth_bid_usd=db, depth_ask_usd=da, imbalance=imb, **kw)


def test_calm_no_events():
    det = EventDetector("BTCUSDT")
    for i in range(60):
        assert det.feed(mk(1000.0 + i)) == []
    assert det.close_all() == []


def test_imbalance_spike_bullish():
    det = EventDetector("BTCUSDT")
    evs = []
    for i in range(30):
        evs += det.feed(mk(1000 + i))
    for i in range(20):
        evs += det.feed(mk(1030 + i, imb=0.5, bid_added_usd=500_000))
    for i in range(10):
        evs += det.feed(mk(1050 + i, imb=0.0))
    assert len(evs) == 1
    e = evs[0]
    assert e["kind"] == "imbalance" and e["direction"] == "bullish"
    assert e["imb_peak"] == 0.5
    assert e["flow_usd"]["bid_added"] > 0
    assert "bullish" in e["label"]
    assert e["duration_sec"] > 0
    assert e["ts_open"] < e["ts_close"]


def test_imbalance_bearish_with_taker():
    det = EventDetector("ETHUSDT")
    evs = []
    for i in range(30):
        evs += det.feed(mk(2000 + i, sym="ETHUSDT"))
    for i in range(15):
        evs += det.feed(mk(2030 + i, sym="ETHUSDT", imb=-0.6, ask_added_usd=400_000,
                           taker_sell_usd=1_000_000, taker_buy_usd=100_000))
    for i in range(8):
        evs += det.feed(mk(2045 + i, sym="ETHUSDT"))
    assert len(evs) == 1
    e = evs[0]
    assert e["direction"] == "bearish"
    assert e["taker_usd"]["net"] < 0
    assert e["flow_usd"]["net"] < 0
    assert e["imb_peak"] == -0.6


def test_imbalance_flip_keeps_direction_peak():
    # Event dibuka bullish (+0.5), kemudian flip bearish (-0.6):
    # arah tetap bullish, imb_peak = ekstrem searah open, range merekam flip.
    det = EventDetector("BTCUSDT")
    evs = []
    for i in range(30):
        evs += det.feed(mk(6000 + i))
    for i in range(8):
        evs += det.feed(mk(6030 + i, imb=0.5))
    for i in range(8):
        evs += det.feed(mk(6038 + i, imb=-0.6))
    for i in range(10):
        evs += det.feed(mk(6046 + i, imb=0.0))
    assert len(evs) == 1
    e = evs[0]
    assert e["direction"] == "bullish"
    assert e["imb_peak"] == 0.5
    assert e["imb_range"]["min"] == -0.6


def test_spread_shock():
    det = EventDetector("BTCUSDT")
    evs = []
    for i in range(40):
        evs += det.feed(mk(3000 + i, spr=2.0))
    for i in range(10):
        evs += det.feed(mk(3040 + i, spr=40.0))
    for i in range(10):
        evs += det.feed(mk(3050 + i, spr=2.0))
    e = [x for x in evs if x["kind"] == "spread_shock"]
    assert len(e) == 1
    assert e[0]["spread_peak_bps"] >= 40


def test_thin_book():
    det = EventDetector("BTCUSDT")
    evs = []
    for i in range(40):
        evs += det.feed(mk(4000 + i, db=1_000_000, da=1_000_000))
    for i in range(10):
        evs += det.feed(mk(4040 + i, db=100_000, da=100_000))
    for i in range(10):
        evs += det.feed(mk(4050 + i, db=1_000_000, da=1_000_000))
    e = [x for x in evs if x["kind"] == "thin_book"]
    assert len(e) == 1
    assert e[0]["depth_min_usd"] <= 200_000


def test_close_all_emits_open_event():
    det = EventDetector("BTCUSDT")
    for i in range(30):
        det.feed(mk(5000 + i))
    for i in range(10):
        det.feed(mk(5030 + i, imb=0.6))
    evs = det.close_all()
    assert len(evs) == 1 and evs[0]["kind"] == "imbalance"


def test_diff_book():
    prev = {"100.0": 5.0, "99.9": 1.0}
    new = {"100.0": 2.0, "99.8": 3.0}
    r = diff_book(prev, new, "bid")
    assert abs(r["removed"] - (3.0 * 100.0 + 1.0 * 99.9)) < 1e-9
    assert abs(r["added"] - 3.0 * 99.8) < 1e-9
    assert r["movers"][0]["side"] == "bid"


def test_slippage_sanity():
    bids = [(100.0, 10), (99.9, 10), (99.8, 10)]
    res = slippage_for_sell(bids, 100.05, 15)
    assert abs(res["filled"] - 15) < 1e-9
    assert 0 < res["slippage_bps"] < 20
    assert res["filled_pct"] == 100.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"ALL {len(fns)} TESTS PASSED")
