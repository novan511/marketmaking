# Liquidity & Order Book Health Dashboard

Portfolio untuk **Liquidity Market Making Senior Associate (CFX)**.

Monitor real-time: spread (bps), 2% market depth, order book imbalance, dan slippage calculator (walk-the-book). Plus continuous imbalance event monitoring (kapan / kenapa / gimana) dengan export.

## Quickstart
```bash
pip install -r requirements.txt
streamlit run app/streamlit_app.py
python3 run_collector.py   # (opsional) continuous event monitor
```

## Backtest (paper trading) — /backtest
Halaman `app/pages/1_backtest.py`: backtest strategi order-flow & imbalance pada
data historis 1 menit, modal virtual $1000, eksekusi **long & short**, rentang
tanggal fleksibel (dibatasi data yang ada).

```bash
python3 fetch_history.py        # unduh data historis 6 bulan (BTCUSDT, ETHUSDT)
streamlit run app/streamlit_app.py   # buka /backtest di sidebar
python3 tests/test_backtest.py       # test engine (10 test)
python3 tests/test_backtest_page.py  # smoke test halaman
```

Data (`data.binance.vision`, publik tanpa API key) per simbol — 6 bulan ≈ 227 ribu bar:
- **klines 1m** — harga + taker buy/sell (order flow agresif)
- **bookDepth 30 detik** — imbalance buku pada ±0.2% / ±1% / ±2%
- **metrics** — open interest + rasio long/short (top trader, agregat, taker)
- **fundingRate** 8 jam + **premiumIndex** (basis futures vs indeks)

`fetch_history.py` menulis `data/history/features_1m_<SYMBOL>.csv` (21 kolom fitur);
zip mentah disimpan di `data/history/raw/` (bisa dihapus untuk hemat 230 MB).

### Strategi bawaan
| Strategi | Logika |
|---|---|
| `taker_flow` | Ikut arah rolling mean taker net USD (korelasi +0.61 dgn harga) |
| `imbalance` | Ikut arah rolling mean imbalance buku 1% (+0.55) |
| `combo` | Long/short hanya jika taker flow **dan** imbalance sepakat |
| `revert` | Melawan imbalance ekstrem (exhaustion / mean reversion) |

Parameter: window rolling, ambang sinyal, minimal tahan posisi (anti-whipsaw),
fee + slippage (bps/sisi), izin long/short. Hasil: equity vs buy&hold, drawdown,
return per bulan, daftar trade (CSV download).

### Asumsi backtest (penting untuk dibaca)
- Sinyal bar **i** dieksekusi di bar **i+1** (tanpa lookahead)
- All-in per posisi; short = paper (margin 1x, tanpa biaya pinjaman/likuidasi)
- Biaya: fee + slippage per sisi, default 5 + 1 bps (realistis taker futures)
- **Bukan nasihat keuangan.** Backtest 1 menit tidak merepresentasikan slippage
  order besar, likuidasi derivatif, atau regim pasar baru (overfitting risk).
  Untuk sinyal realistis, validasikan out-of-sample dan tambah variabel
  cross-market (funding, OI, basis) yang sudah tersedia di kolom data.

## Continuous Monitoring (Collector)
Proses terpisah dari Streamlit, jalan terus untuk mendeteksi event imbalance:
**kapan** terjadi, **kenapa** (order flow + taker flow), **gimana** (timeline), dan bisa **diexport**.

```bash
python3 run_collector.py            # foreground
nohup python3 run_collector.py &    # background
python3 export_report.py            # export events + report ke data/exports/
python3 tests/test_events.py        # sanity test
```

Auto-start saat boot (macOS, jalan terus & auto-restart kalau mati):
```bash
cp deploy/com.novandri.mm-collector.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.novandri.mm-collector.plist
launchctl unload ~/Library/LaunchAgents/com.novandri.mm-collector.plist   # stop
```

Tuning: `--tick 1.0 --depth-limit 100 --imb-open 0.35 --imb-close 0.20 --persist 5 --no-trades`.

### Mode in-app (Streamlit Cloud)
Jika tidak ada collector eksternal yang aktif, app menjalankan **in-app collector**
(thread background di dalam proses Streamlit, tick 2s) sehingga versi online tetap
mengekspor event selama instance-nya hidup. Batasan Cloud: instance tidur saat
browser ditutup -> in-app berhenti dan data ephemeral hilang. Untuk 24/7 tetap
pakai `run_collector.py` di mesin sendiri/VPS. Tab "Event & Surveillance"
menampilkan mode mana yang aktif (eksternal vs in-app).

### Deteksi event
| Event | Trigger |
|---|---|
| `imbalance` | \|imbalance\| ≥ 0.35 selama ~5s (tick 1s); tutup saat < 0.20 selama ~5s |
| `spread_shock` | spread ≥ 3× median 60s dan ≥ 5 bps |
| `thin_book` | total depth 2% ≤ 50% median 60s |

### Data & export
- `data/series.csv` — 1 baris/detik/symbol (mid, spread, depth, imbalance, order flow, taker flow)
- `data/events.jsonl` — event + atribusi lengkap (flow, taker, top movers, timeline, label)
- `data/heartbeat.json` — status collector (ditampilkan di tab "Event & Surveillance")
- `data/exports/` — hasil export dari `python3 export_report.py` (CSV/JSON/report.md)
- Tab "Event & Surveillance" di Streamlit juga punya tombol download langsung

### "Kenapa" imbalance — apa yang bisa diketahui dari data publik
Atribusi bersifat mekanis: order flow (sisi mana yang menambah/mencabut USD), taker flow (agresif buy vs sell), top level movers, pergerakan harga & spread. Penyebab fundamental (berita/makro) tidak bisa dibaca dari order book — pakai `report.md` sebagai dasar anotasi manual.

Data quality: metrik dihitung dari top-N level (default 100). Untuk book sangat tebal, window 2% bisa terpotong (kolom `truncated`=1) sehingga depth jadi lower bound.

## Struktur
- `spec-mvp.md` — spec MVP
- `src/` — client + metrik + slippage + deteksi event + backtest engine (pure functions, mudah di-test)
- `app/streamlit_app.py` — UI (tab Live Dashboard + Event & Surveillance)
- `app/pages/1_backtest.py` — halaman /backtest (paper trading)
- `run_collector.py` — continuous event collector
- `fetch_history.py` — download data historis (klines/bookDepth/metrics/funding/premium)
- `export_report.py` — export event/series (CSV/JSON/report.md)
- `tests/` — sanity tests (event, backtest engine, halaman, app)
- `deploy/` — launchd plist untuk auto-start

## Data
Binance public + Hyperliquid public (fallback UI), tanpa API key. Symbols: BTCUSDT, ETHUSDT, SOLUSDT.

## Interpretasi (untuk non-tech)
- Spread kecil (bps rendah) = likuiditas bagus, biaya trader kecil.
- 2% depth besar = market tahan order besar tanpa gerak harga jauh.
- Imbalance >0 = tekanan beli dominan.
