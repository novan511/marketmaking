# Liquidity & Order Book Health Dashboard — Spec MVP

## 1. Tujuan
Portfolio untuk role **Liquidity Market Making Senior Associate (CFX)**.
Membuktikan bisa: monitor order book, spread, market depth, hitung slippage, dan bikin report likuiditas.

Mapping ke jobdesc CFX:
- Monitor order books, spreads, market depth -> spread bps chart + 2% depth
- Prepare performance reports -> tabel KPI + export CSV
- Basic API + data tools -> Binance public REST/WS, Python + Streamlit

## 2. Scope MVP (3 aset)
Symbols: `BTCUSDT, ETHUSDT, SOLUSDT`
Source: Binance public, tanpa API key.
- REST: `GET https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=100`
- WS (live): `wss://stream.binance.com:9443/ws/btcusdt@depth20@100ms`
- Fallback polling REST 2s jika WS putus.

Out of scope MVP: Hyperliquid, auto-trading, auth, database.

## 3. Metrik Inti (definisi)
1. `mid = (best_bid + best_ask) / 2`
2. `spread_bps = (ask - bid) / mid * 10000`
3. `depth_2pct_bid = sum(price*qty) untuk bid dengan price >= mid*0.98`
4. `depth_2pct_ask = sum(price*qty) untuk ask dengan price <= mid*1.02`
5. `imbalance = (bid_vol_1pct - ask_vol_1pct) / (bid_vol_1pct + ask_vol_1pct)` range [-1,1]
6. Slippage walk-the-book (market sell X unit):
   iterasi level bid dari atas, akumulasi qty sampai terpenuhi.
   `avg_px = sum(px*qty_filled)/total`, `slippage_bps = (mid - avg_px)/mid*10000`
   Jika depth kurang -> return `filled_pct < 100%` + warning.

## 4. Fitur MVP
F1. Header KPI per simbol: mid, spread bps, 2% bid depth ($), 2% ask depth ($), imbalance.
F2. Chart spread bps real-time (rolling 5 menit).
F3. Bar 2% depth bid vs ask.
F4. Depth chart kumulatif (bid/ask).
F5. Slippage calculator: input simbol + size (ex: 50 BTC) + side, output avg px, slippage bps, filled %.
F6. Log tabel snapshot 1 detik + tombol Export CSV (untuk simulasi "regular report").
F7. Status koneksi + latency.

Nice-to-have (post-MVP): tab surveillance (Ide 2), multi-exchange compare.

## 5. Tech Stack
Python 3.11, `streamlit, plotly, pandas, requests, websocket-client`
Run lokal: `streamlit run app/streamlit_app.py`
Deploy: Streamlit Cloud (gratis).

## 6. Kriteria Selesai
- [ ] 3 simbol live <3s lag
- [ ] Spread chart update tiap detik
- [ ] Slippage 50 BTC di BTCUSDT menghasilkan angka masuk akal
- [ ] Export CSV berisi timestamp,mid,spread_bps,depth_bid_2pct,depth_ask_2pct
- [ ] README ada screenshot + cara run + interpretasi untuk non-tech

## 7. Plan Eksekusi
1. `src/binance_client.py` — fetch order book
2. `src/metrics.py` — mid, spread, depth, imbalance
3. `src/slippage.py` — walk-the-book
4. `app/streamlit_app.py` — UI
5. Uji manual + screenshot untuk README
