"""Liquidity & Order Book Health Dashboard (MVP)."""
import io
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA_FILE = ROOT / "data" / "snapshots.csv"

from src.binance_client import fetch_depth as fetch_binance  # noqa: E402
from src.hyperliquid_client import fetch_depth as fetch_hl  # noqa: E402
from src.config import POLL_INTERVAL_SEC, ROLLING_WINDOW_SEC, SYMBOLS  # noqa: E402
from src.metrics import depth_2pct, imbalance, mid_price, spread_bps  # noqa: E402
from src.slippage import slippage_for_buy, slippage_for_sell  # noqa: E402

st.set_page_config(page_title="Liquidity & Order Book Health", layout="wide")
st.title("Liquidity & Order Book Health Dashboard")
st.caption("Public order book — Binance / Hyperliquid. Spread, 2% depth, imbalance, slippage.")

# ---- sidebar ----
source = st.sidebar.selectbox("Source", ["Binance", "Hyperliquid"], index=0)
symbol = st.sidebar.selectbox("Symbol", SYMBOLS, index=0)
limit = st.sidebar.selectbox("Depth limit (kecil = lebih cepat)", [20, 50, 100], index=0)
auto = st.sidebar.checkbox("Auto-refresh", value=True)
poll = st.sidebar.slider("Refresh (detik)", 5, 30, 10)
st.sidebar.divider()
side = st.sidebar.selectbox("Slippage side", ["SELL (market sell ke bid)", "BUY (market buy ke ask)"])
size = st.sidebar.number_input("Slippage size (coin)", min_value=0.001, value=1.0, step=1.0)
if st.sidebar.button("Refresh sekarang"):
    st.rerun()
if st.sidebar.button("Reset history"):
    st.session_state.pop("spread_hist", None)
    st.session_state.pop("snapshots", None)
    st.session_state.pop("_hydrated", None)
    try:
        if DATA_FILE.exists():
            DATA_FILE.unlink()
    except Exception:
        pass
    st.rerun()


def _load_persisted():
    # Dipanggil sekali tiap buka halaman: ambil history dari file agar refresh tidak mulai dari nol.
    if st.session_state.get("_hydrated"):
        return
    st.session_state["_hydrated"] = True
    try:
        if DATA_FILE.exists():
            df = pd.read_csv(DATA_FILE).tail(2000)
            st.session_state["snapshots"] = df.to_dict("records")
            hist = []
            for _, r in df.iterrows():
                try:
                    hist.append({"ts": datetime.fromisoformat(str(r["timestamp"])),
                                 "symbol": r.get("symbol"), "source": r.get("source"),
                                 "spread_bps": float(r.get("spread_bps", 0)),
                                 "mid": float(r.get("mid", 0))})
                except Exception:
                    continue
            st.session_state["spread_hist"] = hist
    except Exception:
        pass


def _save_persisted():
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(st.session_state.get("snapshots", [])).tail(2000).to_csv(DATA_FILE, index=False)
    except Exception:
        pass


_load_persisted()


@st.fragment(run_every=poll if auto else None)
def dashboard():
    # ---- fetch ----
    try:
        t0 = time.time()
        fetcher = fetch_binance if source == "Binance" else fetch_hl
        raw = fetcher(symbol, limit=limit)
        latency_ms = (time.time() - t0) * 1000
        bids = [(float(p), float(q)) for p, q in raw["bids"]]
        asks = [(float(p), float(q)) for p, q in raw["asks"]]
        error = None
    except Exception as e:
        bids, asks, error, latency_ms = [], [], str(e), 0.0

    if error:
        st.error(f"Gagal ambil order book ({source}): {error}")
        st.info("Coba: 1) ganti Source ke Hyperliquid/Binance, 2) Depth limit = 20, 3) matikan VPN, 4) klik Refresh sekarang.")
        return

    if not bids or not asks:
        st.warning("Order book kosong, klik Refresh sekarang.")
        return

    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = mid_price(best_bid, best_ask)
    spr = spread_bps(best_bid, best_ask)
    d_bid, d_ask = depth_2pct(bids, asks, mid)
    imb = imbalance(bids, asks, mid)
    now = datetime.now()

    # ---- history ----
    hist = st.session_state.setdefault("spread_hist", [])
    hist.append({"ts": now, "symbol": symbol, "source": source, "spread_bps": spr, "mid": mid})
    cutoff = now.timestamp() - ROLLING_WINDOW_SEC
    st.session_state["spread_hist"] = [h for h in hist if h["ts"].timestamp() >= cutoff]
    hist = st.session_state["spread_hist"]

    snaps = st.session_state.setdefault("snapshots", [])
    snaps.append({"timestamp": now.isoformat(timespec="seconds"), "source": source, "symbol": symbol,
                   "mid": round(mid, 2), "spread_bps": round(spr, 2),
                   "depth_bid_2pct_usd": round(d_bid, 0), "depth_ask_2pct_usd": round(d_ask, 0),
                   "imbalance": round(imb, 3)})
    st.session_state["snapshots"] = snaps[-500:]
    _save_persisted()

    # ---- KPIs ----
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Mid price", f"${mid:,.2f}")
    c2.metric("Spread", f"{spr:.2f} bps")
    c3.metric("2% Bid depth", f"${d_bid:,.0f}")
    c4.metric("2% Ask depth", f"${d_ask:,.0f}")
    c5.metric("Imbalance (±1%)", f"{imb:+.3f}")
    st.caption(f"{source} — Best bid {best_bid:,.2f} / Best ask {best_ask:,.2f} — latency {latency_ms:.0f} ms — {now.strftime('%H:%M:%S')}")

    # ---- charts ----
    left, right = st.columns(2)
    with left:
        st.subheader("Spread real-time (bps)")
        dfh = pd.DataFrame([h for h in hist if h["symbol"] == symbol and h.get("source") == source])
        fig = go.Figure()
        if not dfh.empty:
            fig.add_trace(go.Scatter(x=dfh["ts"], y=dfh["spread_bps"], mode="lines+markers", name="spread"))
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                          xaxis_title="", yaxis_title="bps")
        st.plotly_chart(fig, width="stretch")
    with right:
        st.subheader("2% Market Depth (USD)")
        fig2 = go.Figure([go.Bar(x=["Bid (≤2%)", "Ask (≤2%)"], y=[d_bid, d_ask])])
        fig2.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig2, width="stretch")

    st.subheader("Cumulative depth")
    bids_sorted = sorted(bids, key=lambda x: -x[0])
    asks_sorted = sorted(asks, key=lambda x: x[0])
    cum_b, px_b, acc = [], [], 0.0
    for p, q in bids_sorted:
        acc += q
        px_b.append(p)
        cum_b.append(acc)
    cum_a, px_a, acc = [], [], 0.0
    for p, q in asks_sorted:
        acc += q
        px_a.append(p)
        cum_a.append(acc)
    fig3 = go.Figure()
    fig3.add_trace(go.Scatter(x=px_b, y=cum_b, mode="lines", name="Bid cum"))
    fig3.add_trace(go.Scatter(x=px_a, y=cum_a, mode="lines", name="Ask cum"))
    fig3.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                       xaxis_title="Price", yaxis_title="Cumulative qty")
    st.plotly_chart(fig3, width="stretch")

    # ---- slippage ----
    st.subheader("Slippage calculator (walk-the-book)")
    if side.startswith("SELL"):
        res = slippage_for_sell(bids, mid, size)
    else:
        res = slippage_for_buy(asks, mid, size)
    s1, s2, s3 = st.columns(3)
    s1.metric("Avg execution", f"${res['avg_px']:,.2f}")
    s2.metric("Slippage", f"{res['slippage_bps']:.2f} bps")
    s3.metric("Filled", f"{res['filled_pct']:.1f}% ({res['filled']:.4f})")
    if res["filled_pct"] < 100:
        st.warning(f"Depth top-{limit} tidak cukup untuk {size} {symbol}. Hanya terisi {res['filled_pct']:.1f}%.")

    # ---- log + export ----
    st.subheader("Snapshot log (untuk report)")
    df = pd.DataFrame(st.session_state["snapshots"])
    st.dataframe(df.tail(20), width="stretch")
    st.download_button("Export CSV", df.to_csv(index=False), f"liquidity_{source}_{symbol}.csv", "text/csv")


def allocation_section(fetcher, src_name: str, depth_limit: int):
    st.divider()
    st.subheader("Liquidity allocation across pairs")
    st.caption("Pantau 3 pair sekaligus. Status SEHAT jika spread <= 10 bps dan total depth >= $1 juta.")
    if st.button("Scan semua pair"):
        st.rerun()
    rows = []
    with st.spinner("Scan BTC, ETH, SOL..."):
        for sym in SYMBOLS:
            try:
                raw = fetcher(sym, limit=depth_limit)
                bids = [(float(p), float(q)) for p, q in raw["bids"]]
                asks = [(float(p), float(q)) for p, q in raw["asks"]]
                mid = mid_price(bids[0][0], asks[0][0])
                spr = spread_bps(bids[0][0], asks[0][0])
                db, da = depth_2pct(bids, asks, mid)
                imb = imbalance(bids, asks, mid)
                status = "SEHAT" if spr <= 10 and (db + da) >= 1_000_000 else "CEK"
                rows.append({"symbol": sym, "mid": round(mid, 2), "spread_bps": round(spr, 2),
                             "bid_2pct_usd": round(db, 0), "ask_2pct_usd": round(da, 0),
                             "total_2pct_usd": round(db + da, 0), "imbalance": round(imb, 3),
                             "status": status})
            except Exception as e:
                rows.append({"symbol": sym, "mid": 0, "spread_bps": -1,
                             "bid_2pct_usd": 0, "ask_2pct_usd": 0,
                             "total_2pct_usd": 0, "imbalance": 0, "status": f"ERROR: {e}"})
    df = pd.DataFrame(rows)
    st.dataframe(df.style.map(lambda v: "color: green" if v == "SEHAT" else ("color: red" if v == "CEK" or str(v).startswith("ERROR") else ""),
                              subset=["status"]), width="stretch")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="allocation", index=False)
        pd.DataFrame(st.session_state.get("snapshots", [])).to_excel(w, sheet_name="snapshots", index=False)
    st.download_button("Export Excel ala Metabase", buf.getvalue(),
                       f"allocation_{src_name}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


dashboard()
allocation_section(fetch_binance if source == "Binance" else fetch_hl, source, limit)
