"""Continuous order book collector: metrik + deteksi event + persist ke disk.

Jalan terus (atau --once), sampling semua symbol tiap tick:
  1. fetch depth Binance (failover base, fast-fail agar loop tidak blocked lama)
  2. diff vs book sebelumnya -> order flow (siapa menambah/mencabut, USD)
  3. fetch delta trades -> taker flow (agresif buy vs sell)
  4. hitung metrik + feed EventDetector
  5. append ke data/series.csv, data/events.jsonl, data/heartbeat.json

Weight budget (Binance, limit 6000/min): depth limit=100 (5 w) + aggTrades
limit=1000 (10 w) per symbol per detik = ~3000 w/min untuk 3 symbol. Aman.
"""
import csv
import json
import logging
import os
import signal
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

from src.binance_client import BASES
from src.events import EventConfig, EventDetector, Sample
from src.metrics import depth_2pct, imbalance, mid_price, spread_bps

SERIES_COLUMNS = [
    "ts", "epoch", "source", "symbol", "mid", "spread_bps",
    "depth_bid_2pct_usd", "depth_ask_2pct_usd", "imbalance",
    "bid_added_usd", "bid_removed_usd", "ask_added_usd", "ask_removed_usd",
    "taker_buy_usd", "taker_sell_usd", "flow_complete",
    "latency_ms", "ok", "truncated",
]
SERIES_ROTATE_BYTES = 40 * 1024 * 1024
TRADE_LIMIT = 1000


def diff_book(prev: Dict[str, float], new: Dict[str, float], side: str) -> dict:
    """Diff dua book {price_str: qty}. Returns {"added","removed","movers"} (USD)."""
    added = removed = 0.0
    movers = []
    for px in set(prev) | set(new):
        p = float(px)
        dq = new.get(px, 0.0) - prev.get(px, 0.0)
        if dq == 0:
            continue
        d_usd = dq * p
        if dq > 0:
            added += d_usd
        else:
            removed += -d_usd
        if abs(d_usd) >= 100.0:  # filter noise kecil
            movers.append({"side": side, "px": p, "d_qty": round(dq, 8),
                           "d_usd": round(d_usd, 2),
                           "kind": "added" if dq > 0 else "removed"})
    movers.sort(key=lambda m: -abs(m["d_usd"]))
    return {"added": added, "removed": removed, "movers": movers[:3]}


class Collector:
    def __init__(self, symbols, outdir="data", tick: float = 1.0,
                 depth_limit: int = 100, trades: bool = True,
                 cfg: Optional[EventConfig] = None, source: str = "binance"):
        self.symbols = list(symbols)
        self.outdir = Path(outdir)
        self.logdir = self.outdir / "logs"
        self.tick = tick
        self.depth_limit = depth_limit
        self.trades = trades
        self.source = source
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "mm-collector/1.0"})
        self.detectors = {s: EventDetector(s, cfg) for s in self.symbols}
        self.books = {s: {"bid": {}, "ask": {}} for s in self.symbols}
        self.trade_state = {s: {"last_id": None} for s in self.symbols}
        self.errors: deque = deque(maxlen=500)
        self.last_event_id: Optional[str] = None
        self._stop = False
        self.series_path = self.outdir / "series.csv"
        self.events_path = self.outdir / "events.jsonl"
        self.heartbeat_path = self.outdir / "heartbeat.json"
        self._setup_logging()
        self._ensure_files()

    # ---------- setup ----------
    def _setup_logging(self):
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("collector")
        if not self.log.handlers:
            self.log.setLevel(logging.INFO)
            fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            fh = logging.FileHandler(self.logdir / "collector.log")
            fh.setFormatter(fmt)
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            self.log.addHandler(fh)
            self.log.addHandler(sh)

    def _ensure_files(self):
        self.outdir.mkdir(parents=True, exist_ok=True)
        if self.series_path.exists() and self.series_path.stat().st_size > SERIES_ROTATE_BYTES:
            arch = self.outdir / f"series_archive_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.csv"
            self.series_path.rename(arch)
            self.log.info("rotated %s -> %s", self.series_path.name, arch.name)
        if not self.series_path.exists():
            with open(self.series_path, "w", newline="") as f:
                csv.writer(f).writerow(SERIES_COLUMNS)
        self._series_fh = open(self.series_path, "a", newline="")
        self._series_w = csv.DictWriter(self._series_fh, fieldnames=SERIES_COLUMNS)

    def _install_signals(self):
        def _h(signum, frame):
            self._stop = True
        try:
            signal.signal(signal.SIGINT, _h)
            signal.signal(signal.SIGTERM, _h)
        except ValueError:
            pass

    # ---------- fetch ----------
    def _fetch_depth(self, symbol: str) -> dict:
        last = None
        for base in BASES:
            for _ in range(2):
                try:
                    r = self.session.get(
                        f"{base}/api/v3/depth",
                        params={"symbol": symbol, "limit": self.depth_limit},
                        timeout=4.0,
                    )
                    r.raise_for_status()
                    return r.json()
                except Exception as e:
                    last = e
        raise RuntimeError(f"semua endpoint depth gagal: {last}")

    def _fetch_trades(self, symbol: str) -> Tuple[float, float, bool]:
        """Delta aggregate trades via /api/v3/aggTrades (fromId didukung, weight 10).
        Returns (taker_buy_usd, taker_sell_usd, complete)."""
        st = self.trade_state[symbol]
        params = {"symbol": symbol, "limit": TRADE_LIMIT}
        if st["last_id"] is not None:
            params["fromId"] = st["last_id"]  # inklusif -> dedup di bawah
        r = self.session.get(f"{BASES[0]}/api/v3/aggTrades", params=params, timeout=4.0)
        r.raise_for_status()
        j = r.json()
        if st["last_id"] is None:
            st["last_id"] = j[-1]["a"] if j else None
            return 0.0, 0.0, True
        tb = tsell = 0.0
        for t in j:
            aid = t["a"]
            if aid <= st["last_id"]:
                continue
            notional = float(t["p"]) * float(t["q"])
            if t["m"]:
                tsell += notional   # buyer=maker -> agresornya SELL
            else:
                tb += notional      # agresornya BUY
            if aid > st["last_id"]:
                st["last_id"] = aid
        complete = len(j) < TRADE_LIMIT  # >= limit berarti kemungkinan ada gap
        return tb, tsell, complete

    # ---------- tick ----------
    def _tick(self, sym: str):
        try:
            t0 = time.time()
            raw = self._fetch_depth(sym)
            latency = (time.time() - t0) * 1000
            bids = [(float(p), float(q)) for p, q in raw["bids"]]
            asks = [(float(p), float(q)) for p, q in raw["asks"]]
        except Exception as e:
            self._note_error(sym, f"depth: {e}")
            self.log.warning("%s depth gagal: %s", sym, e)
            return
        if not bids or not asks:
            self._note_error(sym, "book kosong")
            return

        nb = {str(p): q for p, q in bids}
        na = {str(p): q for p, q in asks}
        old = self.books[sym]
        if old["bid"] and old["ask"]:  # pertama kali: tidak ada diff
            db = diff_book(old["bid"], nb, "bid")
            da = diff_book(old["ask"], na, "ask")
        else:
            db = da = {"added": 0.0, "removed": 0.0, "movers": []}
        self.books[sym] = {"bid": nb, "ask": na}

        tb = tsell = 0.0
        flow_complete = 1
        if self.trades:
            try:
                tb, tsell, complete = self._fetch_trades(sym)
                flow_complete = 1 if complete else 0
            except Exception as e:
                self._note_error(sym, f"trades: {e}")

        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = mid_price(best_bid, best_ask)
        spr = spread_bps(best_bid, best_ask)
        d_bid, d_ask = depth_2pct(bids, asks, mid)
        imb = imbalance(bids, asks, mid)
        truncated = bids[-1][0] > mid * 0.98 or asks[-1][0] < mid * 1.02
        movers = sorted(db["movers"] + da["movers"], key=lambda m: -abs(m["d_usd"]))[:3]

        sample = Sample(
            ts=time.time(), symbol=sym, mid=mid, spread_bps=spr,
            depth_bid_usd=d_bid, depth_ask_usd=d_ask, imbalance=imb,
            bid_added_usd=db["added"], bid_removed_usd=db["removed"],
            ask_added_usd=da["added"], ask_removed_usd=da["removed"],
            taker_buy_usd=tb, taker_sell_usd=tsell,
            latency_ms=latency, ok=True, truncated=truncated,
            top_movers=movers,
        )
        for ev in self.detectors[sym].feed(sample):
            self._write_event(ev)
        self._append_series(sample, flow_complete)

    # ---------- persist ----------
    def _append_series(self, s: Sample, flow_complete: int):
        row = {
            "ts": datetime.fromtimestamp(s.ts, tz=timezone.utc).isoformat(timespec="seconds"),
            "epoch": round(s.ts, 3),
            "source": self.source, "symbol": s.symbol,
            "mid": round(s.mid, 6),
            "spread_bps": round(s.spread_bps, 4),
            "depth_bid_2pct_usd": round(s.depth_bid_usd, 2),
            "depth_ask_2pct_usd": round(s.depth_ask_usd, 2),
            "imbalance": round(s.imbalance, 4),
            "bid_added_usd": round(s.bid_added_usd, 2),
            "bid_removed_usd": round(s.bid_removed_usd, 2),
            "ask_added_usd": round(s.ask_added_usd, 2),
            "ask_removed_usd": round(s.ask_removed_usd, 2),
            "taker_buy_usd": round(s.taker_buy_usd, 2),
            "taker_sell_usd": round(s.taker_sell_usd, 2),
            "flow_complete": flow_complete,
            "latency_ms": round(s.latency_ms, 1),
            "ok": 1, "truncated": 1 if s.truncated else 0,
        }
        self._series_w.writerow(row)
        self._series_fh.flush()

    def _write_event(self, ev: dict):
        self.last_event_id = ev["event_id"]
        with open(self.events_path, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.log.info("EVENT %s %s %s %s dur=%.0fs peak=%+.2f %s",
                      ev["event_id"], ev["symbol"], ev["kind"],
                      ev.get("direction", ""), ev["duration_sec"],
                      ev.get("imb_peak", 0.0), ev["label"])

    def _write_heartbeat(self):
        now = time.time()
        errs = sum(1 for ts, _ in self.errors if now - ts <= 60)
        hb = {
            "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
            "epoch": now, "pid": os.getpid(), "symbols": self.symbols,
            "tick_sec": self.tick, "errors_60s": errs,
            "last_event_id": self.last_event_id,
        }
        tmp = self.outdir / "heartbeat.json.tmp"
        tmp.write_text(json.dumps(hb))
        tmp.replace(self.heartbeat_path)

    def _note_error(self, sym: str, msg: str):
        self.errors.append((time.time(), f"{sym}: {msg}"))

    # ---------- main loop ----------
    def run(self, once: bool = False):
        self._install_signals()
        self.log.info("collector start: %s, tick=%.1fs, depth=%d, trades=%s, outdir=%s",
                      ",".join(self.symbols), self.tick, self.depth_limit,
                      self.trades, self.outdir)
        self._write_heartbeat()
        while not self._stop:
            t0 = time.time()
            for sym in self.symbols:
                if self._stop:
                    break
                self._tick(sym)
            self._write_heartbeat()
            if once:
                break
            elapsed = time.time() - t0
            time.sleep(max(0.0, self.tick - elapsed))
        for det in self.detectors.values():
            for ev in det.close_all():
                self._write_event(ev)
        self._series_fh.close()
        self.log.info("collector stop")
