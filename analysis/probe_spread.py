"""Quick spread probe — uji H1: apakah ada pair/venue dengan spread >= 5 bps sustain?

Poll best bid/ask dari 3 venue selama N menit, lalu hitung distribusi spread (bps)
+ likuiditas top-of-book. Tujuannya jawab kasar H1 hari ini, bukan penelitian final.

  python3 analysis/probe_spread.py --minutes 5
  python3 analysis/probe_spread.py --minutes 0.5     # smoke test

Output:
  - tabel ringkasan ke stdout
  - data/probe/spread_samples_<ts>.csv (baris kasar, untuk analisis lanjut)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import statistics
import sys
import time
from typing import Dict, List, Optional

import requests

UA = {"User-Agent": "spread-probe/0.1 (research)"}

SPOT_INFO = "https://api.binance.com/api/v3/exchangeInfo"
SPOT_24HR = "https://api.binance.com/api/v3/ticker/24hr"
SPOT_BT = "https://api.binance.com/api/v3/ticker/bookTicker"
FUT_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUT_24HR = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FUT_BT = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
HL_INFO = "https://api.hyperliquid.xyz/info"

# Fallback bila resolver lokal (1.1.1.1/8.8.8.8 di jaringan ini) memblokir domain
# Binance dengan NXDOMAIN. DoH menjawab normal -> kita override hanya getaddrinfo
# untuk host tsbi, sehingga SNI + validasi sertifikat tetap pakai hostname asli.
DOH_HOSTS = [
    "api.binance.com",
    "fapi.binance.com",
    "data-api.binance.vision",
    "stream.binance.com",
]
DNS_OVERRIDE: Dict[str, str] = {}
_ORIG_GAI = socket.getaddrinfo

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
HL_MAJOR = ["BTC", "ETH", "SOL"]


def _gai_with_override(host, port, *args, **kwargs):  # noqa: ANN001
    if isinstance(host, str) and host in DNS_OVERRIDE:
        host = DNS_OVERRIDE[host]
    return _ORIG_GAI(host, port, *args, **kwargs)


def doh_resolve(name: str) -> Optional[str]:
    try:
        r = requests.get(
            "https://dns.google/resolve",
            params={"name": name, "type": "A"},
            timeout=8,
            headers=UA,
        )
        j = r.json()
        if j.get("Status") != 0:
            return None
        ips = [a["data"] for a in j.get("Answer", []) if a.get("type") == 1]
        return ips[0] if ips else None
    except Exception:  # noqa: BLE001
        return None


def install_dns_fallback(hosts: List[str]) -> None:
    """Aktifkan override hanya untuk host yang memang tidak bisa di-resolve lokal."""
    socket.getaddrinfo = _gai_with_override
    for h in hosts:
        try:
            _ORIG_GAI(h, 443, socket.AF_INET, socket.SOCK_STREAM)
            continue  # resolvable lokal, tidak perlu override
        except OSError:
            pass
        ip = doh_resolve(h)
        if ip:
            DNS_OVERRIDE[h] = ip
            print(f"  DNS lokal gagal untuk {h} -> pakai DoH {ip}")
        else:
            print(f"  ! {h}: tidak bisa di-resolve (lokal maupun DoH)", file=sys.stderr)


def get_json(url: str, payload: Optional[dict] = None, timeout: float = 15.0):
    try:
        if payload is not None:
            r = requests.post(url, json=payload, timeout=timeout, headers=UA)
        else:
            r = requests.get(url, timeout=timeout, headers=UA)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {url} -> {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- universe ----
def binance_universe(kind: str) -> Dict[str, float]:
    """symbol -> notional volume 24h (USD), hanya pair yang bisa ditradingkan."""
    info = get_json(FUT_INFO if kind == "fut" else SPOT_INFO)
    tick = get_json(FUT_24HR if kind == "fut" else SPOT_24HR)
    if not info or not tick:
        return {}
    tradable = set()
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        if kind == "fut" and s.get("contractType") != "PERPETUAL":
            continue
        tradable.add(s["symbol"])
    vol = {}
    for t in tick:
        sym = t.get("symbol")
        if sym in tradable:
            try:
                vol[sym] = float(t.get("quoteVolume") or 0.0)
            except (TypeError, ValueError):
                vol[sym] = 0.0
    return vol


def hl_universe() -> Dict[str, float]:
    j = get_json(HL_INFO, {"type": "metaAndAssetCtxs"})
    if not j or not isinstance(j, list) or len(j) < 2:
        return {}
    # struktur: [{"universe": [...], ...}, [ctx, ctx, ...]] — index-aligned
    meta, ctxs = j[0], j[1]
    universe = meta.get("universe") or []
    if not isinstance(ctxs, list):
        return {}
    out = {}
    for u, c in zip(universe, ctxs):
        if not isinstance(u, dict) or u.get("isDelisted"):
            continue
        name = u.get("name")
        if not name or not isinstance(c, dict):
            continue
        try:
            out[name] = float(c.get("dayNtlVlm") or 0.0)
        except (TypeError, ValueError):
            out[name] = 0.0
    return out


def pick_buckets(ranked: List[str], per_bucket: int = 5) -> List[str]:
    """Ambil sampel merata lintas likuiditas: dari hampir-mayor sampai tail tipis."""
    ranges = [(4, 16), (40, 70), (100, 150), (180, 260)]
    picked: List[str] = []
    for lo, hi in ranges:
        seg = ranked[lo:hi]
        if not seg:
            continue
        step = max(1, len(seg) // per_bucket)
        picked.extend(seg[::step][:per_bucket])
    return picked


# ----------------------------------------------------------------- polling ----
def binance_snapshot(kind: str) -> List[dict]:
    """Satu request -> semua symbol. Weight: spot 2, fut 40."""
    j = get_json(FUT_BT if kind == "fut" else SPOT_BT, timeout=10)
    if not j:
        return []
    out = []
    for row in j:
        try:
            bid = float(row["bidPrice"])
            ask = float(row["askPrice"])
            bq = float(row["bidQty"])
            aq = float(row["askQty"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        out.append(
            {
                "symbol": row["symbol"],
                "mid": mid,
                "spread_bps": (ask - bid) / mid * 1e4,
                "bid_usd": bid * bq,
                "ask_usd": ask * aq,
            }
        )
    return out


def hl_snapshot(coins: List[str]) -> List[dict]:
    out = []
    for coin in coins:
        j = get_json(HL_INFO, {"type": "l2Book", "coin": coin}, timeout=10)
        if not j:
            continue
        levels = j.get("levels") or [[], []]
        bids, asks = levels[0], levels[1]
        if not bids or not asks:
            continue
        try:
            bid = float(bids[0]["px"])
            bq = float(bids[0]["sz"])
            ask = float(asks[0]["px"])
            aq = float(asks[0]["sz"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        out.append(
            {
                "symbol": coin,
                "mid": mid,
                "spread_bps": (ask - bid) / mid * 1e4,
                "bid_usd": bid * bq,
                "ask_usd": ask * aq,
            }
        )
    return out


# ------------------------------------------------------------------ report ----
def pct(vals: List[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def summarize(rows: List[dict], vols: Dict[str, Dict[str, float]]) -> None:
    groups: Dict[tuple, List[dict]] = {}
    for r in rows:
        groups.setdefault((r["venue"], r["symbol"]), []).append(r)

    print()
    print("=" * 108)
    print("H1 SPREAD PROBE — distribusi spread (bps) & likuiditas top-of-book")
    print("=" * 108)
    hdr = (
        f"{'venue':<7} {'symbol':<14} {'n':>4} {'p50':>8} {'p90':>8} {'p99':>8} "
        f"{'>=3bps':>8} {'>=5bps':>8} {'topBookUSD':>12} {'vol24h$M':>10}"
    )
    print(hdr)
    print("-" * 108)

    summary = []
    for (venue, sym), rs in groups.items():
        sp = [r["spread_bps"] for r in rs]
        bu = statistics.median(r["bid_usd"] for r in rs)
        au = statistics.median(r["ask_usd"] for r in rs)
        vol = vols.get(venue, {}).get(sym, float("nan"))
        summary.append(
            {
                "venue": venue,
                "symbol": sym,
                "n": len(sp),
                "p50": pct(sp, 0.50),
                "p90": pct(sp, 0.90),
                "p99": pct(sp, 0.99),
                "ge3": 100 * sum(1 for x in sp if x >= 3.0) / len(sp),
                "ge5": 100 * sum(1 for x in sp if x >= 5.0) / len(sp),
                "book": (bu + au) / 2,
                "vol": vol / 1e6 if vol == vol else float("nan"),
            }
        )

    # urutkan: dulu yang p50-nya besar (kandidat H1), tapi majors tetap kelihatan
    summary.sort(key=lambda d: d["p50"], reverse=True)
    for d in summary:
        flag = "  <== H1" if d["p50"] >= 5.0 else ("  <- amber" if d["p50"] >= 3.0 else "")
        print(
            f"{d['venue']:<7} {d['symbol']:<14} {d['n']:>4} {d['p50']:>8.3f} "
            f"{d['p90']:>8.3f} {d['p99']:>8.3f} {d['ge3']:>7.1f}% {d['ge5']:>7.1f}% "
            f"{d['book']:>12,.0f} {d['vol']:>10,.1f}{flag}"
        )

    print("-" * 108)
    hit5 = [d for d in summary if d["p50"] >= 5.0]
    amb = [d for d in summary if 3.0 <= d["p50"] < 5.0]
    print(f"Kandidat p50 >= 5 bps : {len(hit5)}/{len(summary)}")
    print(f"Amber  p50 >= 3 bps   : {len(amb)}/{len(summary)}")
    majors = [d for d in summary if d["symbol"].replace("USDT", "") in ("BTC", "ETH", "SOL")]
    if majors:
        print(
            "Baseline majors      : "
            + ", ".join(f"{d['symbol']}({d['venue']}) p50={d['p50']:.3f}bps" for d in majors)
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Quick spread probe (H1)")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--interval", type=float, default=2.0, help="detik antar poll binance")
    ap.add_argument("--hl-interval", type=float, default=3.0)
    ap.add_argument("--per-bucket", type=int, default=4)
    args = ap.parse_args()

    end = time.time() + args.minutes * 60
    print("Memuat universe ...")
    install_dns_fallback(DOH_HOSTS)

    spot_vol = binance_universe("spot")
    fut_vol = binance_universe("fut")
    hl_vol = hl_universe()
    if not spot_vol and not fut_vol and not hl_vol:
        print("Semua universe gagal dimuat —cek koneksi/geo-restriction.", file=sys.stderr)
        return 1

    spot_ranked = [s for s, _ in sorted(spot_vol.items(), key=lambda kv: -kv[1])]
    fut_ranked = [s for s, _ in sorted(fut_vol.items(), key=lambda kv: -kv[1])]
    hl_ranked = [s for s, _ in sorted(hl_vol.items(), key=lambda kv: -kv[1])]

    spot_sel = list(dict.fromkeys(MAJORS + pick_buckets(spot_ranked, args.per_bucket)))
    fut_sel = list(dict.fromkeys(MAJORS + pick_buckets(fut_ranked, args.per_bucket)))
    hl_sel = list(dict.fromkeys(HL_MAJOR + pick_buckets(hl_ranked, max(2, args.per_bucket - 1))))

    print(f"spot : {len(spot_sel)} pair dari {len(spot_ranked)}")
    print(f"fut  : {len(fut_sel)} pair dari {len(fut_ranked)}")
    print(f"hl   : {len(hl_sel)} coin dari {len(hl_ranked)}")
    print(f"Polling selama {args.minutes:.1f} menit ...")

    rows: List[dict] = []
    t0 = time.time()
    n_iter = 0
    next_binance = t0
    next_hl = t0

    try:
        while time.time() < end:
            now = time.time()
            if now >= next_binance:
                ts = time.time()
                for venue, sel in (("spot", spot_sel), ("fut", fut_sel)):
                    snap = binance_snapshot(venue)
                    keep = set(sel)
                    for r in snap:
                        if r["symbol"] in keep:
                            rows.append({"venue": venue, "ts": ts, **r})
                next_binance = time.time() + args.interval
                n_iter += 1
                if n_iter % 10 == 0:
                    el = time.time() - t0
                    print(f"  ... {el:5.0f}s, {len(rows)} sampel", flush=True)
            if time.time() >= next_hl:
                ts = time.time()
                for r in hl_snapshot(hl_sel):
                    rows.append({"venue": "hyper", "ts": ts, **r})
                next_hl = time.time() + args.hl_interval
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nTerinterrupt — tetap menyimpan.")

    if not rows:
        print("Tidak ada sampel.", file=sys.stderr)
        return 1

    vols = {"spot": spot_vol, "fut": fut_vol, "hyper": hl_vol}
    summarize(rows, vols)

    os.makedirs("data/probe", exist_ok=True)
    path = f"data/probe/spread_samples_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["venue", "ts", "symbol", "mid", "spread_bps", "bid_usd", "ask_usd"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} sampel disimpan -> {path}")
    print(f"Durasi: {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
