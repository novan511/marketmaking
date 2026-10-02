"""Imbalance event detection & attribution (pure functions, no I/O).

Feed `Sample` (satu per tick per symbol) ke `EventDetector`. Detector menjalankan
tiga state machine (imbalance / spread shock / thin book) dan memancarkan event
yang sudah ditutup, lengkap dengan atribusi mekanis:
  - order flow: sisi mana (bid/ask) yang menambah/mencabut size, dalam USD
  - taker flow: notional agresif buy vs sell selama event
  - pergerakan harga & spread
  - top level movers + timeline singkat
  - label manusia (Bahasa Indonesia)

Catatan penting: atribusi dari data publik bersifat MEKANIS. Penyebab
fundamental (berita/makro) tidak bisa disimpulkan dari order book saja.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Dict, List, Optional


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Sample:
    ts: float                      # epoch detik (UTC)
    symbol: str
    mid: float
    spread_bps: float
    depth_bid_usd: float           # window 2% (lower bound jika book tertash)
    depth_ask_usd: float
    imbalance: float               # qty-based [-1, 1], + = bid dominan
    bid_added_usd: float = 0.0     # vs sampel sebelumnya
    bid_removed_usd: float = 0.0
    ask_added_usd: float = 0.0
    ask_removed_usd: float = 0.0
    taker_buy_usd: float = 0.0     # tick ini
    taker_sell_usd: float = 0.0
    latency_ms: float = 0.0
    ok: bool = True
    truncated: bool = False        # True jika window 2% tidak tercakup penuh
    top_movers: List[dict] = field(default_factory=list)
    # top_movers: {"side", "px", "d_qty", "d_usd", "kind"}


@dataclass
class EventConfig:
    imb_open_abs: float = 0.35     # |imbalance| untuk membuka event
    imb_close_abs: float = 0.20    # hysteresis untuk menutup
    persist: int = 5               # sampel beruntun (tick 1s => ~5 detik)
    spread_shock_mult: float = 3.0
    spread_shock_min_bps: float = 5.0
    thin_depth_ratio: float = 0.5  # total depth 2% <= 50% baseline
    thin_recover_ratio: float = 0.9
    base_window: int = 60          # baseline rolling (tick 1s => 60s)
    base_warmup: int = 10          # jangan deteksi sebelum baseline stabil
    max_event_sec: float = 600.0   # paksa tutup setelah 10 menit
    timeline_min_usd: float = 10000.0


class EventDetector:
    """Per-symbol state machine. `feed(sample)` -> list event yang tertutup."""

    def __init__(self, symbol: str, cfg: Optional[EventConfig] = None):
        self.symbol = symbol
        self.cfg = cfg or EventConfig()
        self.buf: List[Sample] = []
        self.states: Dict[str, dict] = {}
        self.last: Optional[Sample] = None
        self._seq = 0

    # ---------- public ----------
    def feed(self, s: Sample) -> List[dict]:
        if not s.ok:
            return []
        self.last = s
        self.buf.append(s)
        cap = self.cfg.base_window * 2
        if len(self.buf) > cap:
            self.buf = self.buf[-cap:]
        out = []
        for kind in ("imbalance", "spread_shock", "thin_book"):
            ev = self._step(kind, s)
            if ev is not None:
                out.append(ev)
        return out

    def close_all(self) -> List[dict]:
        """Emits event yang masih terbuka (dipanggil saat shutdown)."""
        out = []
        for kind in list(self.states):
            st = self.states.pop(kind)
            if "open_ts" not in st or self.last is None:
                continue
            out.append(self._close(kind, st, self.last))
        return out

    # ---------- state machine ----------
    def _step(self, kind: str, s: Sample) -> Optional[dict]:
        st = self.states.get(kind)
        if st is None:
            if len(self.buf) < self.cfg.base_warmup:
                return None
            if self._armed(kind, self._value(kind, s), self._open_thr(kind, s)):
                self.states[kind] = {"pending": 1, "close_pending": 0}
            return None

        if "open_ts" not in st:  # masih armed, menunggu persist
            if self._armed(kind, self._value(kind, s), self._open_thr(kind, s)):
                st["pending"] += 1
                if st["pending"] >= self.cfg.persist:
                    self._open(kind, st, s)
            else:
                del self.states[kind]
            return None

        self._accumulate(kind, st, s)
        if not self._inside(kind, self._value(kind, s), self._close_thr(kind, s)):
            st["close_pending"] += 1
        else:
            st["close_pending"] = 0
        if st["close_pending"] >= self.cfg.persist or (s.ts - st["open_ts"]) >= self.cfg.max_event_sec:
            ev = self._close(kind, st, s)
            del self.states[kind]
            return ev
        return None

    def _open(self, kind: str, st: dict, s: Sample):
        st.update({
            "kind": kind, "open_ts": s.ts, "pending": None, "close_pending": 0,
            "flow": {"bid_added": 0.0, "bid_removed": 0.0, "ask_added": 0.0, "ask_removed": 0.0},
            "taker": {"buy": 0.0, "sell": 0.0},
            "movers": [], "timeline": [],
            "spread_open_bps": s.spread_bps, "spread_peak_bps": s.spread_bps,
            "peak_val": self._value(kind, s),
            "mid_open": s.mid, "imb_open": s.imbalance, "imb_peak": s.imbalance,
            "imb_max": s.imbalance, "imb_min": s.imbalance,
            "depth_open_usd": s.depth_bid_usd + s.depth_ask_usd,
            "depth_min_usd": s.depth_bid_usd + s.depth_ask_usd,
            "direction": "bullish" if s.imbalance >= 0 else "bearish",
        })

    def _accumulate(self, kind: str, st: dict, s: Sample):
        f = st["flow"]
        f["bid_added"] += s.bid_added_usd
        f["bid_removed"] += s.bid_removed_usd
        f["ask_added"] += s.ask_added_usd
        f["ask_removed"] += s.ask_removed_usd
        st["taker"]["buy"] += s.taker_buy_usd
        st["taker"]["sell"] += s.taker_sell_usd
        if s.top_movers:
            st["movers"].extend({**m, "ts": s.ts} for m in s.top_movers)
            st["movers"] = sorted(st["movers"], key=lambda m: -abs(m["d_usd"]))[:10]
        top = s.top_movers[0] if s.top_movers else None
        if top and abs(top["d_usd"]) >= self.cfg.timeline_min_usd:
            st["timeline"].append({
                "ts": s.ts,
                "text": f"{top['side']} {top['d_qty']:+,.1f} unit @ {top['px']:,.2f} ({top['kind']}, {top['d_usd']:+,.0f} USD)",
            })
            st["timeline"] = st["timeline"][-12:]
        v = self._value(kind, s)
        st["peak_val"] = max(st["peak_val"], v)
        st["spread_peak_bps"] = max(st["spread_peak_bps"], s.spread_bps)
        if kind == "imbalance":
            st["imb_max"] = max(st["imb_max"], s.imbalance)
            st["imb_min"] = min(st["imb_min"], s.imbalance)
        if kind == "thin_book":
            st["depth_min_usd"] = min(st["depth_min_usd"], s.depth_bid_usd + s.depth_ask_usd)

    def _close(self, kind: str, st: dict, s: Sample) -> dict:
        self._seq += 1
        day = datetime.fromtimestamp(st["open_ts"], tz=timezone.utc).strftime("%Y%m%d")
        hhmmss = datetime.fromtimestamp(st["open_ts"], tz=timezone.utc).strftime("%H%M%S")
        f = st["flow"]
        ev = {
            "event_id": f"EVT-{day}-{hhmmss}-{self.symbol}-{self._seq:04d}",
            "kind": kind, "symbol": self.symbol,
            "ts_open": _iso(st["open_ts"]), "ts_close": _iso(s.ts),
            "duration_sec": round(s.ts - st["open_ts"], 1),
            "flow_usd": {
                "bid_added": round(f["bid_added"], 2), "bid_removed": round(f["bid_removed"], 2),
                "ask_added": round(f["ask_added"], 2), "ask_removed": round(f["ask_removed"], 2),
                "net": round((f["bid_added"] - f["bid_removed"]) - (f["ask_added"] - f["ask_removed"]), 2),
            },
            "taker_usd": {
                "buy": round(st["taker"]["buy"], 2), "sell": round(st["taker"]["sell"], 2),
                "net": round(st["taker"]["buy"] - st["taker"]["sell"], 2),
            },
            "top_movers": st["movers"][:6],
            "timeline": st["timeline"],
            "spread_open_bps": round(st["spread_open_bps"], 2),
            "spread_peak_bps": round(st["spread_peak_bps"], 2),
            "spread_close_bps": round(s.spread_bps, 2),
        }
        if kind == "imbalance":
            peak = st["imb_max"] if st["direction"] == "bullish" else st["imb_min"]
            ev.update({
                "direction": st["direction"],
                "imb_open": round(st["imb_open"], 3),
                "imb_peak": round(peak, 3),
                "imb_range": {"max": round(st["imb_max"], 3), "min": round(st["imb_min"], 3)},
                "imb_close": round(s.imbalance, 3),
                "mid_open": round(st["mid_open"], 6),
                "mid_close": round(s.mid, 6),
                "price_move_bps": round((s.mid - st["mid_open"]) / st["mid_open"] * 10000, 2)
                                  if st["mid_open"] else 0.0,
            })
            ev["label"] = self._label_imbalance(ev)
        elif kind == "spread_shock":
            ev.update({
                "imb_open": round(st["imb_open"], 3),
                "imb_close": round(s.imbalance, 3),
                "price_move_bps": round((s.mid - st["mid_open"]) / st["mid_open"] * 10000, 2)
                                  if st["mid_open"] else 0.0,
            })
            ev["label"] = (f"Spread shock: {ev['spread_open_bps']:.1f} -> peak "
                           f"{ev['spread_peak_bps']:.1f} bps selama {ev['duration_sec']:.0f}s. "
                           f"Taker net {ev['taker_usd']['net']:+,.0f} USD. "
                           f"Harga {ev['price_move_bps']:+.1f} bps.")
        else:
            ev.update({
                "depth_open_usd": round(st["depth_open_usd"], 0),
                "depth_min_usd": round(st["depth_min_usd"], 0),
                "depth_close_usd": round(s.depth_bid_usd + s.depth_ask_usd, 0),
            })
            ev["label"] = (f"Order book tipis: total 2% depth turun dari "
                           f"{ev['depth_open_usd']:,.0f} ke {ev['depth_min_usd']:,.0f} USD selama "
                           f"{ev['duration_sec']:.0f}s, lalu kembali {ev['depth_close_usd']:,.0f} USD.")
        return ev

    # ---------- threshold helpers ----------
    def _value(self, kind: str, s: Sample) -> float:
        if kind == "imbalance":
            return abs(s.imbalance)
        if kind == "spread_shock":
            return s.spread_bps
        return s.depth_bid_usd + s.depth_ask_usd

    def _baseline(self, what: str) -> Optional[float]:
        w = self.buf[-self.cfg.base_window:]
        if len(w) < 5:
            return None
        if what == "spread":
            return median(x.spread_bps for x in w)
        return median(x.depth_bid_usd + x.depth_ask_usd for x in w)

    def _open_thr(self, kind: str, s: Sample) -> float:
        if kind == "imbalance":
            return self.cfg.imb_open_abs
        if kind == "spread_shock":
            base = self._baseline("spread")
            base = base if base is not None else max(s.spread_bps, 2.0)
            return max(self.cfg.spread_shock_mult * base, self.cfg.spread_shock_min_bps)
        base = self._baseline("depth")
        if base is None:
            return 0.0
        return self.cfg.thin_depth_ratio * base

    def _close_thr(self, kind: str, s: Sample) -> float:
        if kind == "imbalance":
            return self.cfg.imb_close_abs
        if kind == "spread_shock":
            base = self._baseline("spread")
            base = base if base is not None else 2.0
            return max(1.5 * base, 2.5)
        base = self._baseline("depth")
        if base is None:
            return 0.0
        return self.cfg.thin_recover_ratio * base

    def _armed(self, kind: str, v: float, thr: float) -> bool:
        if kind == "thin_book":
            return thr > 0 and v <= thr
        return thr > 0 and v >= thr

    def _inside(self, kind: str, v: float, thr: float) -> bool:
        if kind == "thin_book":
            return thr > 0 and v < thr
        return thr > 0 and v > thr

    # ---------- labels ----------
    def _label_imbalance(self, ev: dict) -> str:
        arah = ("bullish (bid dominan)" if ev["direction"] == "bullish"
                else "bearish (ask dominan)")
        t = ev["taker_usd"]
        f = ev["flow_usd"]
        bid_net = f["bid_added"] - f["bid_removed"]
        ask_net = f["ask_added"] - f["ask_removed"]
        top = ev["top_movers"][0] if ev["top_movers"] else None
        movers = (f"Perubahan level terbesar: {top['side']} {top['d_qty']:+,.1f} unit @ "
                  f"{top['px']:,.2f} ({top['kind']})." if top
                  else "Tanpa perubahan level besar.")
        return (f"Imbalance {arah}: imb {ev['imb_open']:+.2f} -> peak {ev['imb_peak']:+.2f} "
                f"selama {ev['duration_sec']:.0f}s. "
                f"Aliran order: bid net {bid_net:+,.0f} USD vs ask net {ask_net:+,.0f} USD. "
                f"Taker: buy {t['buy']:,.0f} vs sell {t['sell']:,.0f} USD (net {t['net']:+,.0f} USD). "
                f"Spread {ev['spread_open_bps']:.1f} -> peak {ev['spread_peak_bps']:.1f} bps. "
                f"Harga {ev['price_move_bps']:+.1f} bps. {movers}")
