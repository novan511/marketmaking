"""Analisis hasil probe spread — jawab H1: ada net edge setelah fee + adverse selection?

  python3 analysis/analyze_probe.py data/probe/spread_samples_<ts>.csv

Fee round-trip (bps) diambil dari riset fee 2026, lihat README/notes di bawah.
Asumsi adverse selection default 1 bps per cycle (bisa diubah --adverse).
"""
from __future__ import annotations

import argparse
import csv
import socket
import statistics as st
from collections import defaultdict
from typing import Dict, List, Optional

import requests

# Fee round-trip bps per venue (maker, dua sisi)
FEE_RT = {
    "fut": 4.0,       # Binance USDT-M regular: 0.02% + 0.02%
    "hyper": 3.0,     # Hyperliquid perps base: 0.015% + 0.015%
    "spot": 0.0,      # Binance Spot Maker Program (0 maker fee, perlu enrollment)
}
FEE_RT_SPOT_RETAIL = 20.0  # 0.10% + 0.10% — ditampilkan sebagai skenario alternatif
MAKER_REBATE_HL = -0.2     # HL MM rebate tier 1 (>0.5% maker share)

# Filter likuiditas: book terlalu tipis = tak bisa menampung order nyata
MIN_BOOK = 500.0


def load(path: str) -> List[dict]:
    rows = []
    for r in csv.DictReader(open(path)):
        try:
            r["spread_bps"] = float(r["spread_bps"])
            r["bid_usd"] = float(r["bid_usd"])
            r["ask_usd"] = float(r["ask_usd"])
        except (KeyError, ValueError):
            continue
        rows.append(r)
    return rows


def pct(vals: List[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--adverse", type=float, default=1.0, help="adverse selection bps/cycle")
    args = ap.parse_args()

    rows = load(args.csv)
    if not rows:
        print("tidak ada data")
        return 1

    g: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        g[(r["venue"], r["symbol"])].append(r)

    stats = []
    for (venue, sym), rs in g.items():
        sp = [r["spread_bps"] for r in rs]
        book = st.median((r["bid_usd"] + r["ask_usd"]) / 2 for r in rs)
        fee = FEE_RT.get(venue, 99.0)
        net = st.median(sp) - fee - args.adverse
        net_retail = st.median(sp) - FEE_RT_SPOT_RETAIL - args.adverse if venue == "spot" else None
        stats.append(
            dict(venue=venue, sym=sym, n=len(sp), p50=st.median(sp),
                 p90=pct(sp, 0.9), p99=pct(sp, 0.99), book=book, fee=fee,
                 net=net, net_retail=net_retail,
                 ge5=100 * sum(1 for x in sp if x >= 5.0) / len(sp))
        )

    stats.sort(key=lambda d: d["net"], reverse=True)

    print("=" * 112)
    print("H1 VERDICT — net edge per cycle = p50 spread - fee round-trip - adverse selection")
    print(f"adverse selection = {args.adverse:.1f} bps/cycle")
    print("=" * 112)
    print(f"{'venue':<6} {'symbol':<14} {'n':>4} {'p50':>7} {'p90':>7} {'%>=5':>6} "
          f"{'feeRT':>6} {'net':>7} {'topBook$':>10}  catatan")
    print("-" * 112)

    green = []
    for d in stats:
        if d["net"] <= 0:
            continue
        note = ""
        if d["book"] < MIN_BOOK:
            note = "book terlalu tipis"
        else:
            note = "KANDIDAT"
            green.append(d)
        print(f"{d['venue']:<6} {d['sym']:<14} {d['n']:>4} {d['p50']:>7.3f} {d['p90']:>7.3f} "
              f"{d['ge5']:>5.1f}% {d['fee']:>6.1f} {d['net']:>7.3f} {d['book']:>10,.0f}  {note}")

    print("-" * 112)
    pos = sum(1 for d in stats if d["net"] > 0)
    print(f"net > 0 : {pos}/{len(stats)}   |   + book >= ${MIN_BOOK:.0f} : {len(green)}/{len(stats)}")

    spot = [d for d in stats if d["venue"] == "spot" and d["net_retail"] is not None]
    pos_retail = sum(1 for d in spot if d["net_retail"] > 0)
    print(f"\nSkenario Binance spot TANPA MM program (fee 20 bps RT): "
          f"{pos_retail}/{len(spot)} pair masih positif")
    for d in sorted(spot, key=lambda x: x["net_retail"], reverse=True)[:5]:
        print(f"    {d['sym']:<14} p50 {d['p50']:7.3f} -> net {d['net_retail']:+.3f} bps")

    print("\nBaseline (untuk konteks, di luar filter kandidat):")
    for d in sorted(stats, key=lambda x: x["p50"])[:6]:
        print(f"    {d['venue']:<6} {d['sym']:<14} p50 {d['p50']:.4f} bps  net {d['net']:+.3f}")

    if green:
        print("\n>>> H1 TERJAWAB: ADA kandidat. Lanjut ke H2 (markout/adverse selection) "
              "dengan pair di atas.")
    else:
        print("\n>>> H1 TERJAWAB: TIDAK ada kandidat pada sampling 5 menit ini. "
              "Perlu sampling lebih lama / filter berbeda sebelum menyimpulkan final.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
