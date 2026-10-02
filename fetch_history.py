#!/usr/bin/env python3
"""Ambil data historis Binance (data.binance.vision, publik tanpa API key)
-> tabel fitur 1 menit untuk backtest.

Sumber per simbol (USDT-M perpetual):
  - klines 1m            : harga OHLCV + taker buy/sell (order flow agresif)
  - bookDepth (30 detik) : imbalance kedalaman buku pada +-0.2%, +-1%, +-2%
  - metrics (~30 menit)  : open interest + rasio long/short
  - fundingRate (8 jam)  : funding rate perpetual
  - premiumIndexKlines   : basis/premium futures vs indeks (bps)

Pakai:
  python3 fetch_history.py                          # default: BTCUSDT ETHUSDT, 6 bulan terakhir
  python3 fetch_history.py --symbols BTCUSDT --start 2026-07-01 --end 2026-10-01

Output: data/history/features_1m_<SYMBOL>.csv (zip mentah disimpan di data/history/raw/).
"""
import argparse
import calendar
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data"
ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "history" / "raw"
OUT = ROOT / "data" / "history"

KLINES_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
               "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]

FUNDING_COLS = ["calc_time", "funding_interval_hours", "last_funding_rate"]
METRICS_COLS = ["create_time", "symbol", "oi_base", "oi_usd",
                "count_toptrader_ls", "sum_toptrader_ls", "count_ls", "taker_ls_vol"]


# ---------- perencanaan URL ----------

def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


class Task:
    """Satu file unduhan: primary dulu, baru fallback (mis. monthly belum terbit -> daily)."""

    def __init__(self, url, path, fallbacks=None, label=""):
        self.url, self.path, self.fallbacks, self.label = url, path, fallbacks or [], label


def _daily_path(kind: str, sym: str, d: date) -> Path:
    return RAW / kind / sym / f"{sym}-{kind}-{d.isoformat()}.zip"


def _daily_url(kind: str, sym: str, d: date) -> str:
    return f"{BASE}/futures/um/daily/{kind}/{sym}/{sym}-{kind}-{d.isoformat()}.zip"


def plan_klines(kind: str, sym: str, start: date, end: date):
    """kind = 'klines' | 'premiumIndexKlines'. Monthly untuk bulan penuh (fallback daily)."""
    tasks = []
    today = date.today()
    for y, m in _months(start, end):
        m_first, m_last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
        seg_first, seg_last = max(start, m_first), min(end, m_last)
        complete = m_first >= start and m_last <= end and m_last < today
        if complete:
            tag = f"{y:04d}-{m:02d}"
            url = f"{BASE}/futures/um/monthly/{kind}/{sym}/1m/{sym}-1m-{tag}.zip"
            path = RAW / kind / sym / f"{sym}-1m-{tag}.zip"
            fb = []
            for d in _days(seg_first, seg_last):  # monthly belum terbit -> per hari
                u = f"{BASE}/futures/um/daily/{kind}/{sym}/1m/{sym}-1m-{d.isoformat()}.zip"
                fb.append((u, RAW / kind / sym / f"{sym}-1m-{d.isoformat()}.zip"))
            tasks.append(Task(url, path, fb, label=f"{kind} {tag}"))
        else:
            for d in _days(seg_first, seg_last):
                url = f"{BASE}/futures/um/daily/{kind}/{sym}/1m/{sym}-1m-{d.isoformat()}.zip"
                path = RAW / kind / sym / f"{sym}-1m-{d.isoformat()}.zip"
                tasks.append(Task(url, path, label=f"{kind} {d}"))
    return tasks


def plan_daily(kind: str, sym: str, start: date, end: date):
    return [Task(_daily_url(kind, sym, d), _daily_path(kind, sym, d), label=f"{kind} {d}")
            for d in _days(start, end)]


def plan_funding(sym: str, start: date, end: date):
    tasks = []
    for y, m in _months(start, end):
        tag = f"{y:04d}-{m:02d}"
        url = f"{BASE}/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{tag}.zip"
        path = RAW / "fundingRate" / sym / f"{sym}-fundingRate-{tag}.zip"
        tasks.append(Task(url, path, label=f"fundingRate {tag}"))
    return tasks


# ---------- unduhan ----------

def _valid_zip(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 100 and zipfile.is_zipfile(path)
    except OSError:
        return False


def _get(url: str, path: Path) -> bool:
    if _valid_zip(path):
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=90)
            if r.status_code != 200:
                return False  # 404 = file belum terbit / tidak ada
            if not r.content.startswith(b"PK"):
                return False
            tmp = path.with_suffix(".part")
            tmp.write_bytes(r.content)
            if zipfile.is_zipfile(tmp):
                tmp.replace(path)
                return True
            tmp.unlink(missing_ok=True)
            return False
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return False


def run_task(t: Task) -> tuple:
    if _get(t.url, t.path):
        return True, t.label, t.url
    for url, path in t.fallbacks:  # monthly belum terbit -> coba daily per hari
        if _get(url, path):
            return True, t.label, url
    return False, t.label, t.url


def download(tasks, workers=8):
    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_task, t) for t in tasks]
        for f in as_completed(futs):
            ok, label, url = f.result()
            done += 1
            if not ok:
                failed += 1
                print(f"  [SKIP 404] {label}")
            elif done % 50 == 0:
                print(f"  ... {done}/{len(tasks)} file")
    print(f"  unduhan selesai: {done - failed} ok, {failed} tidak tersedia (404)")


# ---------- baca zip -> DataFrame ----------

def read_zip(path: Path, names: list = None) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        member = sorted(z.namelist())[0]
        with z.open(member) as f:
            head = f.read(256).decode("utf-8", "replace")
            f.seek(0)
            first = head.split(",", 1)[0].strip().strip('"')
            has_header = bool(first) and (first[0].isalpha() or first[0] == "_")
            if has_header:
                return pd.read_csv(f)
            df = pd.read_csv(f, header=None)
    if names and len(names) >= len(df.columns):
        df.columns = names[: len(df.columns)]
    return df


def _to_epoch_sec(s) -> pd.Series:
    """Timestamp string 'YYYY-MM-DD HH:MM:SS' (UTC) atau epoch ms -> epoch detik."""
    if pd.api.types.is_numeric_dtype(s):
        return (s.astype("int64") // 1000).astype("int64")
    return (pd.to_datetime(s, utc=True, errors="coerce").astype("int64") // 1_000_000_000).astype("int64")


def load_klines(sym: str) -> pd.DataFrame:
    """Base per menit: harga + taker flow."""
    files = sorted((RAW / "klines" / sym).glob("*.zip")) + \
            sorted((RAW / "premiumIndexKlines" / sym).glob("*.zip"))
    if not files:
        return pd.DataFrame()
    ks, ps = [], []
    for p in sorted((RAW / "klines" / sym).glob("*.zip")):
        df = read_zip(p, KLINES_COLS)
        df = df[["open_time", "open", "high", "low", "close", "volume",
                 "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"]].copy()
        ks.append(df)
    for p in sorted((RAW / "premiumIndexKlines" / sym).glob("*.zip")):
        df = read_zip(p, KLINES_COLS)[["open_time", "close"]].copy()
        df.columns = ["open_time", "basis_close"]
        ps.append(df)
    if not ks:
        return pd.DataFrame()
    k = pd.concat(ks, ignore_index=True)
    k["ts"] = (k["open_time"].astype("int64") // 1000).astype("int64")
    for c in ["open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_volume", "taker_buy_quote_volume"]:
        k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.drop_duplicates("ts").sort_values("ts")
    # order flow agresif: net = buy agresif - sell agresif (dalam USD)
    k["taker_sell_volume"] = k["volume"] - k["taker_buy_volume"]
    k["taker_net_usd"] = 2 * k["taker_buy_quote_volume"] - k["quote_volume"]
    out = k[["ts", "open", "high", "low", "close", "volume",
             "taker_buy_volume", "taker_sell_volume", "taker_net_usd"]].reset_index(drop=True)
    if ps:
        b = pd.concat(ps, ignore_index=True)
        b["ts"] = (b["open_time"].astype("int64") // 1000).astype("int64")
        b["basis_close"] = pd.to_numeric(b["basis_close"], errors="coerce")
        b = b.drop_duplicates("ts").sort_values("ts")
        out = pd.merge_asof(out, b[["ts", "basis_close"]], on="ts", direction="backward")
        out = out.rename(columns={"basis_close": "basis_raw"})
    return out


def load_bookdepth(sym: str) -> pd.DataFrame:
    """Imbalance kedalaman buku (per 30 detik) dari notional pada level persentase."""
    frames = []
    for p in sorted((RAW / "bookDepth" / sym).glob("*.zip")):
        df = read_zip(p)
        if "percentage" not in df.columns or "notional" not in df.columns:
            continue
        df["ts"] = _to_epoch_sec(df["timestamp"])
        df["percentage"] = pd.to_numeric(df["percentage"], errors="coerce")
        df["notional"] = pd.to_numeric(df["notional"], errors="coerce")
        piv = df.pivot_table(index="ts", columns="percentage", values="notional", aggfunc="first")
        frames.append(piv)
    if not frames:
        return pd.DataFrame()
    piv = pd.concat(frames).sort_index()
    piv = piv[~piv.index.duplicated(keep="last")]
    out = pd.DataFrame({"ts": piv.index.astype("int64")})
    out.index = piv.index  # samakan index agar alignment benar
    for p in (0.2, 1.0, 2.0):
        if -p in piv.columns and p in piv.columns:
            bid, ask = piv[-p].astype(float), piv[p].astype(float)
            out[f"imb_{str(p).replace('.', 'p')}"] = (bid - ask) / (bid + ask)
            if p == 1.0:
                out["depth_bid_1pct_usd"] = bid
                out["depth_ask_1pct_usd"] = ask
    return out.reset_index(drop=True)


def load_metrics(sym: str) -> pd.DataFrame:
    # nama kolom asli dari header file dump metrics
    rename = {"sum_open_interest_value": "oi_usd",
              "sum_toptrader_long_short_ratio": "toptrader_ls",
              "count_long_short_ratio": "ls_ratio",
              "sum_taker_long_short_vol_ratio": "taker_ls_ratio"}
    frames = []
    for p in sorted((RAW / "metrics" / sym).glob("*.zip")):
        df = read_zip(p, METRICS_COLS)
        if "create_time" not in df.columns:
            continue
        df["ts"] = _to_epoch_sec(df["create_time"])
        df = df.rename(columns=rename)
        keep = ["ts"] + [c for c in rename.values() if c in df.columns]
        frames.append(df[keep])
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    for c in out.columns:
        if c != "ts":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def load_funding(sym: str) -> pd.DataFrame:
    frames = []
    for p in sorted((RAW / "fundingRate" / sym).glob("*.zip")):
        df = read_zip(p, FUNDING_COLS)
        if "calc_time" not in df.columns:
            continue
        df["ts"] = _to_epoch_sec(df["calc_time"])
        df["funding_rate"] = pd.to_numeric(df["last_funding_rate"], errors="coerce")
        frames.append(df[["ts", "funding_rate"]])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")


# ---------- gabung -> fitur 1 menit ----------

def build_features(sym: str, start: date, end: date) -> pd.DataFrame:
    base = load_klines(sym)
    if base.empty:
        raise SystemExit(f"{sym}: tidak ada klines mentah — jalankan unduhan dulu.")
    lo = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    hi = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    base = base[(base["ts"] >= lo) & (base["ts"] <= hi)].reset_index(drop=True)

    for name, loader in [("bookDepth", load_bookdepth), ("metrics", load_metrics),
                         ("fundingRate", load_funding)]:
        ext = loader(sym)
        if ext.empty:
            print(f"  [{sym}] {name}: tidak ada data (dilewati)")
            continue
        base = pd.merge_asof(base, ext, on="ts", direction="backward")

    base["ret_1m_bps"] = base["close"].pct_change() * 1e4
    if "basis_raw" in base.columns:
        # premium index disimpan sbg ratio -> bps
        base["basis_bps"] = base["basis_raw"] * 1e4
        base = base.drop(columns=["basis_raw"])
    return base


def main():
    ap = argparse.ArgumentParser(description="Download data historis Binance untuk backtest")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: 6 bulan lalu)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: kemarin)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else (end - timedelta(days=183)).replace(day=1)
    print(f"Periode: {start} -> {end} | simbol: {', '.join(args.symbols)}")

    all_tasks = []
    for sym in args.symbols:
        all_tasks += plan_klines("klines", sym, start, end)
        all_tasks += plan_klines("premiumIndexKlines", sym, start, end)
        all_tasks += plan_daily("bookDepth", sym, start, end)
        all_tasks += plan_daily("metrics", sym, start, end)
        all_tasks += plan_funding(sym, start, end)
    print(f"{len(all_tasks)} file direncanakan; mengunduh (skip jika sudah ada)...")
    download(all_tasks, workers=args.workers)

    for sym in args.symbols:
        print(f"Membangun fitur {sym}...")
        df = build_features(sym, start, end)
        OUT.mkdir(parents=True, exist_ok=True)
        out = OUT / f"features_1m_{sym}.csv"
        df.to_csv(out, index=False)
        if df.empty:
            print(f"  [WARN] {out} kosong")
        else:
            t0 = datetime.fromtimestamp(int(df.ts.min()), tz=timezone.utc).date()
            t1 = datetime.fromtimestamp(int(df.ts.max()), tz=timezone.utc).date()
            print(f"  {out.name}: {len(df):,} baris, {t0} -> {t1}, "
                  f"{df.columns.size} kolom")


if __name__ == "__main__":
    sys.exit(main())
