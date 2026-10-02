#!/usr/bin/env python3
"""Entry point: continuous imbalance & liquidity collector.

Contoh:
  python3 run_collector.py                  # 3 symbol, tick 1s, Binance
  python3 run_collector.py --tick 2 --no-trades
  python3 run_collector.py --once           # sekali jalan (untuk test)
  nohup python3 run_collector.py &          # jalan terus di background
"""
import argparse
from pathlib import Path

from src.collector import Collector
from src.config import SYMBOLS
from src.events import EventConfig


def main():
    ap = argparse.ArgumentParser(description="Collector imbalance & liquidity (continuous)")
    ap.add_argument("--tick", type=float, default=1.0, help="interval sampling per symbol (detik)")
    ap.add_argument("--depth-limit", type=int, default=100)
    ap.add_argument("--symbols", default=",".join(SYMBOLS), help="pisahkan dengan koma")
    ap.add_argument("--outdir", default=None, help="default: <root>/data")
    ap.add_argument("--no-trades", action="store_true", help="skip taker flow (hemat API weight)")
    ap.add_argument("--once", action="store_true", help="sekali jalan lalu stop")
    ap.add_argument("--imb-open", type=float, default=0.35, help="threshold |imbalance| buka event")
    ap.add_argument("--imb-close", type=float, default=0.20, help="threshold hysteresis tutup event")
    ap.add_argument("--persist", type=int, default=5, help="banyak tick beruntun untuk open/close")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    outdir = Path(args.outdir) if args.outdir else root / "data"
    cfg = EventConfig(imb_open_abs=args.imb_open, imb_close_abs=args.imb_close, persist=args.persist)
    col = Collector(
        symbols=[s.strip() for s in args.symbols.split(",") if s.strip()],
        outdir=outdir, tick=args.tick, depth_limit=args.depth_limit,
        trades=not args.no_trades, cfg=cfg,
    )
    col.run(once=args.once)


if __name__ == "__main__":
    main()
