"""Halaman Backtest (paper trading) — akses via /backtest.

Backtest strategi order-flow & imbalance pada data historis 1 menit
(data/history/features_1m_<SYMBOL>.csv, hasil `python3 fetch_history.py`).
Semua eksekusi PAPER: modal virtual (default $1000), long & short, tanpa order riil.
"""
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.backtest import DEFAULT_THRESHOLD, STRATEGIES, build_signal, run_backtest  # noqa: E402

HIST_DIR = ROOT / "data" / "history"

st.set_page_config(page_title="Backtest", page_icon="📉", layout="wide")
st.title("📉 Backtest — paper trading")
st.caption("Strategi order-flow & imbalance pada data historis 1 menit. "
           "Semua simulasi PAPER (modal virtual) — tidak ada order riil.")


# ---------- util ----------

@st.cache_data(show_spinner="Memuat data historis...")
def load_history(symbol: str) -> pd.DataFrame:
    path = HIST_DIR / f"features_1m_{symbol}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def available_symbols() -> list:
    return sorted(p.stem.replace("features_1m_", "")
                  for p in HIST_DIR.glob("features_1m_*.csv"))


def _fmt_ts(epoch) -> str:
    return pd.to_datetime(epoch, unit="s").strftime("%Y-%m-%d %H:%M")


# ---------- sidebar: parameter ----------

syms = available_symbols()
if not syms:
    st.error("Belum ada data historis. Jalankan dulu: `python3 fetch_history.py` "
             "(mengunduh klines + bookDepth + metrics dari data.binance.vision).")
    st.stop()

st.sidebar.header("Parameter backtest")
symbol = st.sidebar.selectbox("Symbol", syms, index=0)
capital = st.sidebar.number_input("Modal awal (USD, paper)", min_value=10.0,
                                  value=1000.0, step=100.0, format="%.0f")
strategy = st.sidebar.selectbox(
    "Strategi", list(STRATEGIES.keys()),
    format_func=lambda k: f"{k} — {STRATEGIES[k][1]}")
window = st.sidebar.slider("Window rolling (menit)", 5, 240, 60, step=5)

if strategy != "taker_flow":
    threshold = st.sidebar.slider("Ambang sinyal (imbalance ratio 0–1)",
                                  0.0, 0.60, float(DEFAULT_THRESHOLD[strategy]), step=0.05)
else:
    threshold = st.sidebar.number_input("Ambang taker net (USD)",
                                        min_value=0.0, value=0.0, step=50_000.0)

min_hold = st.sidebar.slider("Minimal tahan posisi (menit)", 0, 1440, 720, step=30)
fee_bps = st.sidebar.number_input("Fee (bps per sisi)", 0.0, 50.0, 5.0, step=0.5)
slip_bps = st.sidebar.number_input("Slippage (bps per sisi)", 0.0, 50.0, 1.0, step=0.5)

c1, c2 = st.sidebar.columns(2)
allow_long = c1.checkbox("Boleh LONG", value=True)
allow_short = c2.checkbox("Boleh SHORT", value=True)
if not allow_long and not allow_short:
    st.sidebar.warning("Long & short sama-sama mati — tidak akan ada trade.")
st.sidebar.caption("Short = paper (margin 1x, tanpa biaya pinjaman). "
                   "Sinyal bar i dieksekusi di bar i+1 (tanpa lookahead).")


# ---------- rentang tanggal (dibatasi data yang ada) ----------

df_all = load_history(symbol)
if df_all.empty or len(df_all) < 60:
    st.error(f"Data `{symbol}` kosong / terlalu pendek. Jalankan `python3 fetch_history.py`.")
    st.stop()

d_min = df_all["dt"].min().date()
d_max = df_all["dt"].max().date()
total_days = (d_max - d_min).days

st.sidebar.divider()
st.sidebar.subheader("Rentang tanggal")
preset = st.sidebar.radio("Preset", ["30 hari terakhir", "90 hari terakhir",
                                     "180 hari terakhir", "Custom"],
                          index=2 if total_days >= 180 else 0)
if preset == "Custom":
    rng = st.sidebar.date_input("Dari — sampai", (d_min, d_max),
                                min_value=d_min, max_value=d_max)
else:
    n = int(preset.split()[0])
    rng = (max(d_min, d_max - timedelta(days=n)), d_max)

run = st.sidebar.button("Jalankan backtest", type="primary",
                        use_container_width=True)


# ---------- hasil (default: rentang terakhir / tombol ditekan) ----------

if "bt_done" not in st.session_state:
    st.session_state["bt_done"] = False

if run:
    st.session_state["bt_done"] = True

if not st.session_state["bt_done"]:
    st.info("Atur parameter di sidebar, lalu tekan **Jalankan backtest**.")
    st.stop()

if not (isinstance(rng, tuple) and len(rng) == 2):
    st.warning("Rentang tanggal harus 'Dari — sampai' (dua tanggal).")
    st.stop()

start_d, end_d = rng
if start_d >= end_d:
    st.warning("Tanggal mulai harus sebelum tanggal akhir.")
    st.stop()

mask = (df_all["dt"].dt.date >= start_d) & (df_all["dt"].dt.date <= end_d)
df = df_all.loc[mask].reset_index(drop=True)
if len(df) < 120:
    st.warning(f"Rentang hanya berisi {len(df)} bar — minimal 120 bar (2 jam) untuk backtest bermakna.")
    st.stop()

with st.spinner("Menjalankan backtest..."):
    sig = build_signal(strategy, df, window=window, threshold=threshold)
    res = run_backtest(df, sig, capital=capital, fee_bps=fee_bps,
                       slippage_bps=slip_bps, allow_long=allow_long,
                       allow_short=allow_short, min_hold=min_hold)
s = res.stats
months = max(s["days"] / 30.44, 1 / 30.44)

# ---------- KPI ----------

st.subheader(f"{symbol} · {strategy} · {start_d} → {end_d} "
             f"({months:.1f} bulan, {len(df):,} bar)")
delta = s["total_return_pct"] - s["buyhold_return_pct"]
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Modal akhir", f"${s['capital_end']:,.2f}",
          f"{s['total_return_pct']:+.2f}%")
k2.metric("Buy & hold", f"{s['buyhold_return_pct']:+.2f}%",
          f"selisih {delta:+.2f} pp", delta_color="off")
k3.metric("Per bulan", f"{s['ret_per_month_pct']:+.2f}%" if s["ret_per_month_pct"] is not None else "—")
k4.metric("Trades", f"{s['n_trades']}",
          f"{s['n_long']} long / {s['n_short']} short", delta_color="off")
k5.metric("Win rate", f"{s['win_rate_pct']:.1f}%",
          f"PF {s['profit_factor'] if s['profit_factor'] is not None else '∞'}",
          delta_color="off")
k6.metric("Max drawdown", f"{s['max_drawdown_pct']:.2f}%",
          f"exposure {s['exposure_pct']:.0f}%", delta_color="off")

if s["n_trades"] == 0:
    st.warning("Tidak ada trade — long/short mungkin keduanya mati, ambang terlalu tinggi, "
               "atau sinyal tidak pernah muncul di rentang ini.")
    st.stop()

# ---------- equity vs buy & hold ----------

st.subheader("Equity curve (paper) vs buy & hold")
eq = res.equity.copy()
eq.index = df["dt"]
eq = eq.resample("60min").last().dropna() if len(eq) > 20_000 else eq
bh = capital * df["close"] / df["close"].iloc[0]
bh.index = df["dt"]
bh = bh.reindex(eq.index, method="ffill")

fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                    row_heights=[0.68, 0.32], subplot_titles=("Modal (USD)", "Harga + eksekusi"))
fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="Equity (strategi)",
                         line=dict(color="#2ecc71", width=2)), row=1, col=1)
fig.add_trace(go.Scatter(x=bh.index, y=bh.values, name="Buy & hold",
                         line=dict(color="#95a5a6", width=1.5, dash="dot")), row=1, col=1)
fig.add_hline(y=capital, line_dash="dot", line_color="#7f8c8d",
              annotation_text=f"Modal ${capital:,.0f}", row=1, col=1)

# marker entry/exit pada harga
tr = pd.DataFrame([t.__dict__ | {"ret_pct": t.ret_pct, "dur": t.duration_min}
                   for t in res.trades])
if not tr.empty:
    px = df["close"]
    long_e = tr[tr["side"] == 1]
    short_e = tr[tr["side"] == -1]
    fig.add_trace(go.Scatter(
        x=[df["dt"].iloc[i] for i in long_e["entry_i"]],
        y=[px.iloc[i] for i in long_e["entry_i"]],
        mode="markers", name="Entry long",
        marker=dict(size=9, symbol="triangle-up", color="#2ecc71",
                    line=dict(width=1, color="white"))), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=[df["dt"].iloc[i] for i in short_e["entry_i"]],
        y=[px.iloc[i] for i in short_e["entry_i"]],
        mode="markers", name="Entry short",
        marker=dict(size=9, symbol="triangle-down", color="#e74c3c",
                    line=dict(width=1, color="white"))), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=[df["dt"].iloc[i] for i in tr["exit_i"]],
        y=[px.iloc[i] for i in tr["exit_i"]],
        mode="markers", name="Exit",
        marker=dict(size=7, symbol="x", color="#f39c12")), row=2, col=1)
    fig.add_trace(go.Scatter(x=df["dt"], y=px, name="Harga",
                             line=dict(color="#3498db", width=1)), row=2, col=1)

fig.update_layout(height=720, margin=dict(l=10, r=10, t=40, b=10),
                  legend=dict(orientation="h", y=1.04))
st.plotly_chart(fig, width="stretch")

# ---------- drawdown ----------

st.subheader("Drawdown")
peak = res.equity.cummax()
dd = ((res.equity - peak) / peak * 100)
dd.index = df["dt"]
if len(dd) > 20_000:
    dd = dd.resample("30min").min().dropna()
fig_dd = go.Figure(go.Scatter(x=dd.index, y=dd.values, fill="tozeroy",
                              line=dict(color="#e74c3c", width=1)))
fig_dd.update_layout(height=220, margin=dict(l=10, r=10, t=10, b=10),
                     yaxis_title="%")
st.plotly_chart(fig_dd, width="stretch")

# ---------- return per bulan ----------

st.subheader("Return per bulan (equity strategi)")
eq_full = res.equity.copy()
eq_full.index = df["dt"]
# groupby periode (bukan resample label bulan-akhir) agar bulan terakhir (parsial) ikut
eq_naive = eq_full.tz_localize(None) if eq_full.index.tz else eq_full
monthly = eq_naive.groupby(eq_naive.index.to_period("M")).last()
close_s = df.set_index("dt")["close"]
close_naive = close_s.tz_localize(None) if close_s.index.tz else close_s
mb = close_naive.groupby(close_naive.index.to_period("M")).last()
mret = pd.DataFrame({
    "Modal akhir": monthly.values,
    "Return %": (monthly.values / capital - 1) * 100,
    "Buy&hold %": (mb.reindex(monthly.index).values / df["close"].iloc[0] - 1) * 100,
}, index=monthly.index.astype(str))
mret["Modal akhir"] = mret["Modal akhir"].map("${:,.2f}".format)
mret["Return %"] = mret["Return %"].map("{:+.2f}%".format)
mret["Buy&hold %"] = mret["Buy&hold %"].map("{:+.2f}%".format)
st.dataframe(mret, width="stretch")

# ---------- daftar trade ----------

st.subheader(f"Daftar trade ({len(tr)})")
tbl = tr.copy()
tbl["Entry"] = [ _fmt_ts(df['ts'].iloc[i]) for i in tbl["entry_i"] ]
tbl["Exit"]  = [ _fmt_ts(df['ts'].iloc[i]) for i in tbl["exit_i"] ]
tbl["Side"]  = tbl["side"].map({1: "LONG", -1: "SHORT"})
tbl["Entry px"] = tbl["entry_px"].map("{:,.2f}".format)
tbl["Exit px"] = tbl["exit_px"].map("{:,.2f}".format)
tbl["PnL $"] = tbl["pnl"].map("{:+,.2f}".format)
tbl["Ret %"] = tbl["ret_pct"].map("{:+.2f}%".format)
tbl["Durasi (menit)"] = tbl["dur"]
show = tbl[["Entry", "Exit", "Side", "Entry px", "Exit px",
            "PnL $", "Ret %", "Durasi (menit)"]].sort_values("Entry", ascending=False)
st.dataframe(show, height=360, width="stretch")
st.download_button("Download trades CSV", show.to_csv(index=False),
                   f"backtest_{symbol}_{strategy}_{start_d}_{end_d}.csv", "text/csv")

st.divider()
st.caption(
    f"**Asumsi:** eksekusi di close bar berikutnya setelah sinyal (T+1), biaya "
    f"{fee_bps:.1f} bps fee + {slip_bps:.1f} bps slippage per sisi, all-in per posisi, "
    f"short paper tanpa biaya pinjaman. **Ini bukan nasihat keuangan** — hasil historis "
    f"tidak menjamin hasil di masa depan; backtest pada data 1 menit tidak merepresentasikan "
    f"slippage order besar, likuiditas, atau pergerakan likuidasi derivatif."
)
