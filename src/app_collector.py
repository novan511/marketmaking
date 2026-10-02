"""In-app collector: thread background di dalam proses Streamlit.

Dipakai agar dashboard mandiri di tempat tanpa proses collector terpisah
(run_collector.py), misalnya Streamlit Cloud:
- Hanya jalan jika TIDAK ada heartbeat eksternal yang fresh. Jika collector
  eksternal (run_collector.py) mulai aktif, thread ini berhenti sendiri.
- Menulis format yang sama: data/series.csv, data/events.jsonl,
  data/heartbeat.json (dengan marker "in_app": true).
- Instance Streamlit Cloud tidur saat browser ditutup -> in-app juga berhenti
  dan file ephemeral hilang. Untuk koleksi 24/7 tetap pakai run_collector.py
  di mesin sendiri (launchd) atau VPS.
"""
import json
import threading
import time
from pathlib import Path
from typing import Optional

from src.collector import Collector
from src.events import EventConfig

_active: Optional["InAppCollector"] = None
_lock = threading.Lock()


def external_heartbeat_fresh(outdir, max_age: float = 15.0) -> bool:
    """True jika ada heartbeat FRESH yang ditulis collector eksternal."""
    try:
        hb = json.loads((Path(outdir) / "heartbeat.json").read_text())
        return time.time() - float(hb["epoch"]) < max_age and not hb.get("in_app")
    except Exception:
        return False


def running_mode(outdir, max_age: float = 15.0) -> str:
    """'external' / 'in-app' / 'none'."""
    if external_heartbeat_fresh(outdir, max_age):
        return "external"
    with _lock:
        if _active is not None and _active.is_alive():
            return "in-app"
    return "none"


def ensure_running(symbols, outdir, tick: float = 2.0, depth_limit: int = 100,
                   cfg: Optional[EventConfig] = None) -> str:
    """Pastikan ada collector aktif. Returns 'external' atau 'in-app'."""
    if external_heartbeat_fresh(outdir):
        return "external"
    global _active
    with _lock:
        if _active is not None and _active.is_alive():
            return "in-app"
        if external_heartbeat_fresh(outdir):
            return "external"
        _active = InAppCollector(symbols=symbols, outdir=outdir, tick=tick,
                                 depth_limit=depth_limit, cfg=cfg)
        _active.start()
        return "in-app"


class InAppCollector:
    """Bungkus Collector sebagai thread daemon yang berhenti sendiri
    ketika collector eksternal terdeteksi aktif."""

    def __init__(self, symbols, outdir, tick: float = 2.0,
                 depth_limit: int = 100, cfg: Optional[EventConfig] = None):
        self.outdir = Path(outdir)
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="mm-inapp-collector")
        self.collector = Collector(symbols=symbols, outdir=self.outdir,
                                   tick=tick, depth_limit=depth_limit,
                                   cfg=cfg, in_app=True)

    def _run(self):
        self.collector.external_check = lambda: external_heartbeat_fresh(self.outdir)
        self.collector.run(once=False)

    def start(self):
        self.thread.start()

    def is_alive(self) -> bool:
        return self.thread.is_alive()
