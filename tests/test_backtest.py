"""Quick sanity tests (tanpa pytest): python3 tests/test_backtest.py"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest import (BacktestResult, run_backtest, signal_combo, signal_imb,
                          signal_revert, signal_taker)


def mk_df(prices, imb=None, taker=None):
    n = len(prices)
    return pd.DataFrame({
        "ts": np.arange(n, dtype=int),
        "close": prices,
        "imb_1p0": imb if imb is not None else [0.0] * n,
        "taker_net_usd": taker if taker is not None else [0.0] * n,
    })


def test_flat_signal_no_trades():
    df = mk_df([100.0] * 50)
    res = run_backtest(df, signal_taker(df), capital=1000)
    assert res.stats["n_trades"] == 0
    assert res.equity.iloc[-1] == 1000.0


def test_long_profit_upward_trend():
    # harga naik tetap + taker net positif -> long -> profit
    prices = [100.0 * (1.001 ** i) for i in range(100)]
    taker = [10_000.0] * 100
    df = mk_df(prices, taker=taker)
    res = run_backtest(df, signal_taker(df), capital=1000, fee_bps=0, slippage_bps=0)
    assert res.stats["n_long"] >= 1
    assert res.stats["n_short"] == 0
    # buy&hold 100 bar * 0.1% ~= +10.5%; all-in tanpa biaya harus lebih tinggi/serupa
    assert res.stats["total_return_pct"] > 5.0
    assert res.stats["capital_end"] > 1000


def test_short_profit_downward_trend():
    prices = [100.0 * (0.999 ** i) for i in range(100)]
    taker = [-10_000.0] * 100
    df = mk_df(prices, taker=taker)
    res = run_backtest(df, signal_taker(df), capital=1000, fee_bps=0, slippage_bps=0)
    assert res.stats["n_short"] >= 1
    assert res.stats["n_long"] == 0
    assert res.stats["total_return_pct"] > 0


def test_direction_flips():
    # taker berbalik -> posisi harus berpindah long -> short
    taker = [10_000.0] * 50 + [-10_000.0] * 50
    prices = [100.0] * 50 + [100.0] * 50  # harga datar -> fokus pada posisi
    df = mk_df(prices, taker=taker)
    res = run_backtest(df, signal_taker(df, window=5), capital=1000,
                       fee_bps=0, slippage_bps=0)
    assert res.stats["n_long"] >= 1
    assert res.stats["n_short"] >= 1


def test_fees_reduce_return():
    prices = [100.0 * (1.001 ** i) for i in range(100)]
    taker = [10_000.0] * 100
    df = mk_df(prices, taker=taker)
    free = run_backtest(df, signal_taker(df), capital=1000, fee_bps=0, slippage_bps=0)
    costly = run_backtest(df, signal_taker(df), capital=1000, fee_bps=10, slippage_bps=5)
    assert costly.stats["capital_end"] < free.stats["capital_end"]


def test_no_lookahead_execution():
    # sinyal hanya muncul di bar terakhir -> tidak boleh ada trade sama sekali
    # (eksekusi T+1, bar terakhir tak punya eksekusi)
    prices = [100.0] * 30
    taker = [0.0] * 29 + [10_000.0]
    df = mk_df(prices, taker=taker)
    res = run_backtest(df, signal_taker(df, window=2), capital=1000)
    assert res.stats["n_trades"] == 0


def test_allow_flags():
    prices = [100.0 * (1.001 ** i) for i in range(60)]
    taker = [10_000.0] * 60
    df = mk_df(prices, taker=taker)
    res = run_backtest(df, signal_taker(df), capital=1000, allow_long=False, allow_short=True)
    assert res.stats["n_long"] == 0


def test_combo_requires_agreement():
    # taker bullish tapi imbalance bearish -> combo harus flat
    n = 60
    df = mk_df([100.0] * n, imb=[-0.5] * n, taker=[10_000.0] * n)
    sig = signal_combo(df, window=5, imb_threshold=0.1)
    assert (sig == 0).all()


def test_revert_signal_opposite():
    n = 60
    df = mk_df([100.0] * n, imb=[0.5] * n)
    sig = signal_revert(df, window=5, threshold=0.3)
    # bar pertama 0 (min_periods rolling) -> setelah warmup harus -1
    assert (sig.iloc[2:] == -1).all()


def test_equity_never_negative_paper():
    # volatilitas ekstrem, sinyal bolak-balik -> equity tak boleh negatif (paper, 1x)
    rng = np.random.default_rng(42)
    prices = list(100 * np.exp(np.cumsum(rng.normal(0, 0.005, 500))))
    taker = list(rng.normal(0, 50_000, 500))
    df = mk_df(prices, taker=taker)
    res = run_backtest(df, signal_taker(df, window=3), capital=1000)
    assert res.equity.min() > 0
    assert res.stats["max_drawdown_pct"] <= 0


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} test lulus.")


if __name__ == "__main__":
    main()
