"""Export artifacts dari data collector (pure: mengembalikan string, tanpa CLI)."""
import csv
import json
from datetime import datetime, timezone
from io import StringIO
from typing import List, Optional


def _hhmmss(ts) -> str:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
    return str(ts)[11:19]

FLAT_EVENT_COLUMNS = [
    "event_id", "ts_open", "ts_close", "duration_sec", "symbol", "kind", "direction",
    "imb_open", "imb_peak", "imb_close", "price_move_bps",
    "spread_open_bps", "spread_peak_bps", "spread_close_bps",
    "taker_buy_usd", "taker_sell_usd", "taker_net_usd", "flow_net_usd", "label",
]


def load_events(jsonl_path) -> List[dict]:
    out = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out


def _flat(ev: dict) -> dict:
    t = ev.get("taker_usd", {})
    f = ev.get("flow_usd", {})
    return {
        "event_id": ev["event_id"], "ts_open": ev["ts_open"], "ts_close": ev["ts_close"],
        "duration_sec": ev["duration_sec"], "symbol": ev["symbol"], "kind": ev["kind"],
        "direction": ev.get("direction", ""),
        "imb_open": ev.get("imb_open", ""), "imb_peak": ev.get("imb_peak", ""),
        "imb_close": ev.get("imb_close", ""), "price_move_bps": ev.get("price_move_bps", ""),
        "spread_open_bps": ev.get("spread_open_bps"),
        "spread_peak_bps": ev.get("spread_peak_bps"),
        "spread_close_bps": ev.get("spread_close_bps"),
        "taker_buy_usd": t.get("buy"), "taker_sell_usd": t.get("sell"),
        "taker_net_usd": t.get("net"), "flow_net_usd": f.get("net"),
        "label": ev.get("label", ""),
    }


def events_csv(events: List[dict]) -> str:
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=FLAT_EVENT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for ev in events:
        w.writerow(_flat(ev))
    return buf.getvalue()


def events_json(events: List[dict]) -> str:
    return json.dumps(events, indent=1, ensure_ascii=False)


def _usd(v) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def report_md(events: List[dict], since: Optional[str] = None, limit: int = 15) -> str:
    evs = [e for e in events if since is None or e["ts_open"] >= since]
    if not evs:
        return "# Laporan Event Imbalance & Liquidity\n\nBelum ada event."
    imb = [e for e in evs if e["kind"] == "imbalance"]
    bull = sum(1 for e in imb if e.get("direction") == "bullish")
    lines = ["# Laporan Event Imbalance & Liquidity", ""]
    lines.append(f"- Periode: {evs[0]['ts_open']} s.d. {evs[-1]['ts_open']} (UTC)")
    lines.append(f"- Total event: {len(evs)} "
                 f"(imbalance {len(imb)}, spread shock {sum(1 for e in evs if e['kind'] == 'spread_shock')}, "
                 f"thin book {sum(1 for e in evs if e['kind'] == 'thin_book')})")
    if imb:
        lines.append(f"- Imbalance: bullish {bull} / bearish {len(imb) - bull}, "
                     f"durasi rata-rata {sum(e['duration_sec'] for e in imb) / len(imb):.0f}s, "
                     f"|imb| peak maks {max(abs(e['imb_peak']) for e in imb):.2f}")
    lines.append("")
    lines.append("## Top Event")
    lines.append("")
    lines.append("| Waktu (UTC) | Symbol | Jenis | Arah | Durasi (s) | Imb peak | Gerak harga (bps) | Taker net (USD) |")
    lines.append("|---|---|---|---|---|---|---|---|")

    def rank(e):
        ip = e.get("imb_peak")
        return (abs(ip) if isinstance(ip, (int, float)) else 0.0, e.get("duration_sec", 0))

    for e in sorted(evs, key=rank, reverse=True)[:limit]:
        ip = e.get("imb_peak", "—")
        pm = e.get("price_move_bps", "—")
        tnet = e.get("taker_usd", {}).get("net")
        lines.append(f"| {e['ts_open']} | {e['symbol']} | {e['kind']} | "
                     f"{e.get('direction', '—')} | {e['duration_sec']:.0f} | {ip} | {pm} | {_usd(tnet)} |")
    lines.append("")
    lines.append("## Detail & Atribusi (Kapan / Kenapa / Gimana)")
    for e in sorted(evs, key=rank, reverse=True)[:limit]:
        lines.append(f"### {e['event_id']} — {e['symbol']} {e['kind']} {e.get('direction', '')}")
        lines.append(f"- **Kapan**: {e['ts_open']} -> {e['ts_close']} ({e['duration_sec']:.0f}s)")
        lines.append(f"- **Kenapa (aliran order)**: {e.get('label', '')}")
        movers = e.get("top_movers") or []
        if movers:
            lines.append("- Top level movers (UTC):")
            for m in movers:
                lines.append(f"  - {_hhmmss(m['ts'])} {m['side']} {m['d_qty']:+,.1f} unit @ "
                             f"{m['px']:,.2f} ({m['kind']}, {m['d_usd']:+,.0f} USD)")
        tl = e.get("timeline") or []
        if tl:
            lines.append("- Timeline (UTC):")
            for s in tl:
                lines.append(f"  - {_hhmmss(s['ts'])} {s['text']}")
        f = e.get("flow_usd", {})
        t = e.get("taker_usd", {})
        lines.append(f"- Order flow: bid +{_usd(f.get('bid_added'))} / -{_usd(f.get('bid_removed'))} USD; "
                     f"ask +{_usd(f.get('ask_added'))} / -{_usd(f.get('ask_removed'))} USD; "
                     f"net {_usd(f.get('net'))} USD")
        lines.append(f"- Taker: buy {_usd(t.get('buy'))} vs sell {_usd(t.get('sell'))} USD "
                     f"(net {_usd(t.get('net'))} USD)")
        lines.append("")
    lines.append("---")
    lines.append("*Catatan: atribusi di atas bersifat mekanis (public order flow + taker flow). "
                 "Penyebab fundamental (berita/makro) tidak bisa disimpulkan dari order book saja — "
                 "pakai report ini sebagai dasar anotasi manual.*")
    return "\n".join(lines)
