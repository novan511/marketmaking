# Liquidity & Order Book Health Dashboard

Portfolio untuk **Liquidity Market Making Senior Associate (CFX)**.

Monitor real-time: spread (bps), 2% market depth, order book imbalance, dan slippage calculator (walk-the-book). Plus continuous imbalance event monitoring (kapan / kenapa / gimana) dengan export.

## Quickstart
```bash
pip install -r requirements.txt
streamlit run app/streamlit_app.py
python3 run_collector.py   # (opsional) continuous event monitor
```

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
- `src/` — client + metrik + slippage + deteksi event (pure functions, mudah di-test)
- `app/streamlit_app.py` — UI (tab Live Dashboard + Event & Surveillance)
- `run_collector.py` — continuous event collector
- `export_report.py` — export event/series (CSV/JSON/report.md)
- `tests/test_events.py` — quick sanity tests
- `deploy/` — launchd plist untuk auto-start

## Data
Binance public + Hyperliquid public (fallback UI), tanpa API key. Symbols: BTCUSDT, ETHUSDT, SOLUSDT.

## Interpretasi (untuk non-tech)
- Spread kecil (bps rendah) = likuiditas bagus, biaya trader kecil.
- 2% depth besar = market tahan order besar tanpa gerak harga jauh.
- Imbalance >0 = tekanan beli dominan.
