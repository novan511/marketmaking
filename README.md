# Liquidity & Order Book Health Dashboard

Portfolio untuk **Liquidity Market Making Senior Associate (CFX)**.

Monitor real-time: spread (bps), 2% market depth, order book imbalance, dan slippage calculator (walk-the-book).

## Quickstart
```bash
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

## Struktur
- `spec-mvp.md` — spec MVP
- `src/` — client + metrik + slippage (pure functions, mudah di-test)
- `app/streamlit_app.py` — UI

## Data
Binance public, tanpa API key. Symbols: BTCUSDT, ETHUSDT, SOLUSDT.

## Interpretasi (untuk non-tech)
- Spread kecil (bps rendah) = likuiditas bagus, biaya trader kecil.
- 2% depth besar = market tahan order besar tanpa gerak harga jauh.
- Imbalance >0 = tekanan beli dominan.
