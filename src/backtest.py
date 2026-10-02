"""Backtest engine (paper trading) — pure functions, tanpa I/O.

Konvensi:
- Input: DataFrame fitur 1 menit dari fetch_history.py (kolom close, taker_net_usd,
  imb_1p0, dst) + sinyal -1/0/+1 dari signal_*().
- Eksekusi T+1: sinyal dari bar i dieksekusi pada bar i+1 (hindari lookahead).
- Modal USD (default 1000), all-in per arah, long ATAU short.
  Short = paper: margin 1x, tanpa biaya pinjaman, tanpa likuidasi.
- Biaya: (fee + slippage) bps per sisi, diterapkan pada harga masuk/keluar.

Strategi (basis fitur yang sama dengan analisis event kita):
- taker_flow : ikut arah rolling mean taker_net_usd (order flow, korelasi +0.61)
- imbalance  : ikut arah rolling mean imbalance buku (+0.55)
- combo      : keduanya harus sepakat (konfirmasi ganda)
- revert     : melawan imbalance ekstrem (exhaustion / mean reversion)
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ---------- sinyal: Series -1/0/+1 ----------

def _rolling_sign(df: pd.DataFrame, col: str, window: int, threshold: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(0, index=df.index, dtype=int)
    m = df[col].rolling(window, min_periods=max(2, window // 2)).mean()
    s = np.sign(m)
    s[(m.abs() < threshold) | ~np.isfinite(m)] = 0.0
    return s.fillna(0.0).astype(int)


def signal_taker(df: pd.DataFrame, window: int = 5, threshold_usd: float = 0.0) -> pd.Series:
    """Ikuti arah order flow agresif (taker net USD)."""
    return _rolling_sign(df, "taker_net_usd", window, threshold_usd)


def signal_imb(df: pd.DataFrame, window: int = 5, threshold: float = 0.10) -> pd.Series:
    """Ikuti arah imbalance buku (kolom imb_1p0, [-1..1])."""
    return _rolling_sign(df, "imb_1p0", window, threshold)


def signal_combo(df: pd.DataFrame, window: int = 5,
                 imb_threshold: float = 0.10, taker_threshold_usd: float = 0.0) -> pd.Series:
    """Long/short hanya jika taker flow DAN imbalance sepakat; selain itu flat."""
    a = signal_taker(df, window, taker_threshold_usd)
    b = signal_imb(df, window, imb_threshold)
    return a.where(a == b, 0).astype(int)


def signal_revert(df: pd.DataFrame, window: int = 5, threshold: float = 0.30) -> pd.Series:
    """Contra: imbalance ekstrem dianggap exhaustion -> lawan arah ketimpangan."""
    return (-signal_imb(df, window, threshold)).astype(int)


STRATEGIES = {
    "taker_flow": (signal_taker, "Ikuti order flow agresif (taker net USD)"),
    "imbalance": (signal_imb, "Ikuti arah imbalance buku 1%"),
    "combo": (signal_combo, "Konfirmasi ganda: taker flow + imbalance harus sepakat"),
    "revert": (signal_revert, "Melawan imbalance ekstrem (exhaustion / mean reversion)"),
}

# ambang default per strategi (makna: USD untuk taker_flow, rasio imb untuk lainnya)
DEFAULT_THRESHOLD = {"taker_flow": 0.0, "imbalance": 0.10, "combo": 0.10, "revert": 0.30}


def build_signal(name: str, df: pd.DataFrame, window: int = 60,
                 threshold: float = None) -> pd.Series:
    """Dispatch sinyal berdasar nama strategi (parameter seragam)."""
    if name not in STRATEGIES:
        raise ValueError(f"strategi tak dikenal: {name}")
    fn, _ = STRATEGIES[name]
    th = DEFAULT_THRESHOLD[name] if threshold is None else threshold
    if name == "taker_flow":
        return fn(df, window=window, threshold_usd=th)
    if name == "combo":
        return fn(df, window=window, imb_threshold=th)
    return fn(df, window=window, threshold=th)


# ---------- backtest ----------

@dataclass
class Trade:
    entry_i: int
    exit_i: int
    side: int            # +1 long, -1 short
    entry_px: float
    exit_px: float
    qty: float
    pnl: float           # USD, setelah biaya
    entry_ts: int
    exit_ts: int

    @property
    def ret_pct(self) -> float:
        notional = self.qty * self.entry_px
        return (self.pnl / notional * 100) if notional else 0.0

    @property
    def duration_min(self) -> int:
        return self.exit_i - self.entry_i


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    signal: pd.Series = None


def run_backtest(df: pd.DataFrame,
                 signal: pd.Series,
                 capital: float = 1000.0,
                 fee_bps: float = 2.0,
                 slippage_bps: float = 1.0,
                 allow_long: bool = True,
                 allow_short: bool = True,
                 min_hold: int = 0,
                 warmup: int = 1) -> BacktestResult:
    """Simulasi all-in long/short per bar.

    min_hold: minimal bar menahan posisi sebelum boleh berbalik/checkout
              (mengurangi whipsaw & biaya fee dari sinyal yang flip cepat).

    Akuntansi:
      long  : masuk  entry_px = px*(1+c), qty = cash/entry_px, cash=0
              keluar cash = qty * px*(1-c)
      short : masuk  entry_px = px*(1-c), qty = cash/entry_px (margin = cash)
              keluar cash = qty*(2*entry_px - px*(1+c))  = margin + qty*(entry-exit)
    """
    if len(df) == 0:
        return BacktestResult(pd.Series(dtype=float), [], {}, None)

    px = df["close"].to_numpy(dtype=float)
    ts = df["ts"].to_numpy(dtype=int)
    sig = signal.reindex(df.index).fillna(0).astype(int).to_numpy()
    n = len(df)
    c = (fee_bps + slippage_bps) / 1e4  # total biaya per sisi (fraksi)

    cash = float(capital)
    pos = 0                 # 0 / +1 / -1
    qty = 0.0
    entry_px = 0.0
    entry_i = 0
    entry_ts = 0
    equity = np.empty(n, dtype=float)
    trades: list[Trade] = []

    def _open(want: int, i: int):
        nonlocal cash, pos, qty, entry_px, entry_i, entry_ts
        entry_px = px[i] * (1 + want * c)
        qty = cash / entry_px
        pos, entry_i, entry_ts = want, i, int(ts[i])
        if want == 1:
            cash = 0.0            # seluruh cash jadi inventory
        # short: cash tetap sebagai margin

    def _close(i: int) -> float:
        nonlocal cash, pos, qty
        exit_px = px[i] * (1 - pos * c)
        if pos == 1:
            cash = qty * exit_px
        else:  # short
            cash = qty * (2 * entry_px - exit_px)
        pnl = cash - cash_at_entry[0]
        t = Trade(entry_i, i, pos, entry_px, exit_px, qty, pnl, entry_ts, int(ts[i]))
        trades.append(t)
        pos, qty = 0, 0.0
        return cash

    cash_at_entry = [float(capital)]

    for i in range(n):
        # eksekusi sinyal bar sebelumnya (T+1)
        if i >= warmup:
            want = int(sig[i - 1])
            if want == 1 and not allow_long:
                want = 0
            if want == -1 and not allow_short:
                want = 0
            held_ok = (pos == 0) or (i - entry_i >= min_hold)
            if want != pos and held_ok:
                if pos != 0:
                    _close(i)
                if want != 0:
                    cash_at_entry[0] = cash
                    _open(want, i)

        # mark-to-market
        if pos == 1:
            equity[i] = qty * px[i]              # cash sudah 0 (jadi inventory)
        elif pos == -1:
            equity[i] = qty * (2 * entry_px - px[i])  # margin + PnL paper
        else:
            equity[i] = cash

    # tutup posisi terbuka di akhir rentang
    if pos != 0:
        _close(n - 1)
        equity[-1] = cash

    eq = pd.Series(equity, index=df.index, name="equity")
    stats = compute_stats(eq, trades, px, capital, df)
    return BacktestResult(eq, trades, stats, signal)


def compute_stats(eq: pd.Series, trades: list, px: np.ndarray,
                  capital: float, df: pd.DataFrame) -> dict:
    final = float(eq.iloc[-1])
    total_ret = final / capital - 1.0
    n_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n_trades if n_trades else 0.0
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    bh = px[-1] / px[0] - 1.0 if len(px) > 1 else 0.0

    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min()) if len(eq) and peak.min() > 0 else 0.0

    days = (int(df["ts"].iloc[-1]) - int(df["ts"].iloc[0])) / 86400 if len(df) > 1 else 0.0
    months = days / 30.44
    ret_per_month = ((1 + total_ret) ** (1 / months) - 1) if months > 0.05 and total_ret > -1 else None

    exposure = sum(t.exit_i - t.entry_i for t in trades) / len(px) if len(px) else 0.0
    avg_win = float(np.mean([t.pnl for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t.pnl for t in losses])) if losses else 0.0
    longs = sum(1 for t in trades if t.side == 1)

    return {
        "capital_start": capital,
        "capital_end": round(final, 2),
        "total_return_pct": round(total_ret * 100, 2),
        "buyhold_return_pct": round(bh * 100, 2),
        "ret_per_month_pct": round(ret_per_month * 100, 2) if ret_per_month is not None else None,
        "n_trades": n_trades,
        "n_long": longs,
        "n_short": n_trades - longs,
        "win_rate_pct": round(win_rate * 100, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exposure_pct": round(exposure * 100, 1),
        "days": round(days, 1),
    }
