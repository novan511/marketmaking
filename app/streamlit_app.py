"""Liquidity & Order Book Health Dashboard (MVP)."""
import io
import json
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
DATA_FILE = DATA_DIR / "snapshots.csv"      # history UI legacy
SERIES_FILE = DATA_DIR / "series.csv"       # collector: time series
EVENTS_FILE = DATA_DIR / "events.jsonl"     # collector: events
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"  # collector: status

from src.binance_client import fetch_depth as fetch_binance  # noqa: E402
from src.export import events_csv, events_json, load_events, report_md  # noqa: E402
from src.hyperliquid_client import fetch_depth as fetch_hl  # noqa: E402
from src.config import POLL_INTERVAL_SEC, ROLLING_WINDOW_SEC, SYMBOLS  # noqa: E402
from src.metrics import depth_2pct, imbalance, mid_price, spread_bps  # noqa: E402
from src.slippage import slippage_for_buy, slippage_for_sell  # noqa: E402

st.set_page_config(page_title="Liquidity & Order Book Health", layout="wide")
st.title("Liquidity & Order Book Health Dashboard")
st.caption("Public order book — Binance / Hyperliquid. Spread, 2% depth, imbalance, slippage + event monitoring.")

# ---- sidebar ----
source = st.sidebar.selectbox("Source", ["Binance", "Hyperliquid"], index=0)
symbol = st.sidebar.selectbox("Symbol", SYMBOLS, index=0)
limit = st.sidebar.selectbox("Depth limit (kecil = lebih cepat)", [100, 50, 20], index=0)
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
st.sidebar.caption("Event & surveillance dijalankan proses terpisah: `python3 run_collector.py` (lihat README).")


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

# ---- collector data helpers ----

def _read_series(hours: float = 6.0) -> pd.DataFrame:
    if not SERIES_FILE.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(SERIES_FILE)
    except Exception:
        # baris terakhir mungkin setengah ditulis (collector sedang menulis)
        try:
            text = SERIES_FILE.read_text().splitlines()
            df = pd.read_csv(StringIO("\n".join(text[:-1])))
        except Exception:
            return pd.DataFrame()
    if df.empty or "epoch" not in df.columns:
        return df
    cutoff = time.time() - hours * 3600
    return df[df["epoch"] >= cutoff]


def _collector_status():
    if not HEARTBEAT_FILE.exists():
        return None
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
        return {"age": time.time() - float(hb["epoch"]), **hb}
    except Exception:
        return None


def events_section():
    st.subheader("Continuous monitoring (collector)")
    hb = _collector_status()
    if hb is None:
        st.warning("Collector belum pernah jalan / tidak ada heartbeat. "
                   "Jalankan: `python3 run_collector.py` (lihat README bagian 'Continuous Monitoring').")
    elif hb["age"] > 15:
        st.warning(f"Heartbeat terakhir {hb['age']:.0f}s lalu — collector kemungkinan tidak aktif. "
                   "Jalankan: `python3 run_collector.py`")
    else:
        extra = f" Event terakhir: {hb['last_event_id']}." if hb.get("last_event_id") else ""
        st.success(f"Collector aktif (pid {hb.get('pid')}), heartbeat {hb['age']:.0f}s lalu, "
                   f"error 60s terakhir: {hb.get('errors_60s', 0)}.{extra}")

    series = _read_series(hours=6)
    events = load_events(EVENTS_FILE)

    f1, f2, f3 = st.columns(3)
    sel_syms = f1.multiselect("Symbol", SYMBOLS, default=SYMBOLS)
    kinds = ["imbalance", "spread_shock", "thin_book"]
    sel_kinds = f2.multiselect("Jenis event", kinds, default=kinds)
    min_dur = f3.number_input("Durasi min (detik)", 0, 600, 0, step=10)

    raw = [e for e in events
           if e["symbol"] in sel_syms and e["kind"] in sel_kinds
           and e.get("duration_sec", 0) >= min_dur]

    if series.empty:
        st.info("Belum ada data series (collector belum menghasilkan data).")
    else:
        s2 = series[series["symbol"].isin(sel_syms)]
        st.subheader("Imbalance (±1% window) — 6 jam terakhir")
        fig = go.Figure()
        for sym in sel_syms:
            d = s2[s2["symbol"] == sym]
            if not d.empty:
                fig.add_trace(go.Scatter(x=d["ts"], y=d["imbalance"], name=sym, mode="lines",
                                         line=dict(width=1.5)))
        for e in raw[-30:]:
            color = "rgba(255,80,80,0.25)" if e.get("direction") == "bearish" else "rgba(80,200,120,0.25)"
            fig.add_vline(x=e["ts_open"], line=dict(color=color, width=2))
        fig.add_hline(y=0.35, line_dash="dot", line_color="orange", annotation_text="open")
        fig.add_hline(y=-0.35, line_dash="dot", line_color="orange")
        fig.add_hline(y=0.2, line_dash="dot", line_color="gray")
        fig.add_hline(y=-0.2, line_dash="dot", line_color="gray")
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                          yaxis_title="imbalance [-1..1]")
        st.plotly_chart(fig, width="stretch")

        st.subheader("Spread (bps) — 6 jam terakhir")
        fig2 = go.Figure()
        for sym in sel_syms:
            d = s2[s2["symbol"] == sym]
            if not d.empty:
                fig2.add_trace(go.Scatter(x=d["ts"], y=d["spread_bps"], name=sym, mode="lines",
                                          line=dict(width=1.5)))
        fig2.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                           yaxis_title="bps")
        st.plotly_chart(fig2, width="stretch")

    if not raw:
        st.info("Belum ada event yang cocok. Event imbalance muncul saat |imbalance| >= 0.35 "
                "berlangsung ~5 detik (atau spread shock / book tipis).")
    else:
        st.subheader(f"Event ({len(raw)})")
        rows = []
        for e in raw:
            t = e.get("taker_usd", {})
            rows.append({
                "Waktu (UTC)": e["ts_open"], "Symbol": e["symbol"], "Jenis": e["kind"],
                "Arah": e.get("direction", "—"), "Durasi (s)": e["duration_sec"],
                "Imb peak": e.get("imb_peak", "—"),
                "Gerak harga (bps)": e.get("price_move_bps", "—"),
                "Taker net (USD)": t.get("net", "—"),
                "Label": e.get("label", ""),
            })
        st.dataframe(pd.DataFrame(rows).tail(100), width="stretch", height=420)
        for e in raw[-10:]:
            head = (f"{e['ts_open']} · {e['symbol']} · {e['kind']} {e.get('direction', '')} · "
                    f"{e['duration_sec']:.0f}s · imb peak {e.get('imb_peak', '—')}")
            with st.expander(head):
                st.caption(e.get("label", ""))
                t = e.get("taker_usd", {})
                f = e.get("flow_usd", {})
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Taker net", f"{t.get('net', 0):+,.0f} USD")
                m2.metric("Order flow net", f"{f.get('net', 0):+,.0f} USD")
                m3.metric("Gerak harga", f"{e.get('price_move_bps', 0):+.1f} bps")
                m4.metric("Spread peak", f"{e.get('spread_peak_bps', 0):.1f} bps")
                if e.get("top_movers"):
                    mv = pd.DataFrame(e["top_movers"]).copy()
                    mv["ts"] = [datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S UTC")
                                for t in mv["ts"]]
                    st.dataframe(mv, width="stretch")

    d1, d2, d3, d4 = st.columns(4)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if events:
        d1.download_button("Export events.csv", events_csv(events), f"events_{ts}.csv", "text/csv")
        d2.download_button("Export events.json", events_json(events), f"events_{ts}.json", "application/json")
        d3.download_button("Export report.md", report_md(events, limit=20), f"report_{ts}.md", "text/markdown")
    if SERIES_FILE.exists():
        d4.download_button("Export series.csv", SERIES_FILE.read_bytes(), "series.csv", "text/csv")


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

    tab_live, tab_events = st.tabs(["Live Dashboard", "Event & Surveillance"])
    with tab_live:
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

    with tab_events:
        events_section()


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
