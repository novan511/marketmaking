"""Smoke test halaman backtest: python3 tests/test_backtest_page.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest


def main():
    at = AppTest.from_file(str(ROOT / "app" / "pages" / "1_backtest.py"), default_timeout=120)
    at.run()
    assert not at.exception, f"exception saat load: {at.exception}"

    # halaman minta tekan tombol dulu
    assert any("Jalankan backtest" in str(x) for x in at.sidebar.button), \
        [str(b) for b in at.sidebar.button]

    at.sidebar.button[0].click()
    at.run()
    assert not at.exception, f"exception saat run: {at.exception}"

    # KPI harus tampil: modal akhir + trades
    metrics = [str(m.label) for m in at.metric]
    print("metrics:", metrics)
    assert any("Modal akhir" in m for m in metrics), metrics
    assert any("Trades" in m for m in metrics), metrics

    # pastikan tidak ada error merah
    assert not at.error, [str(e.value) for e in at.error]

    charts = len(at.get("plotly_chart"))
    tables = len(at.dataframe)
    print(f"charts={charts} tables={tables} metrics={len(metrics)}")
    assert charts >= 2, charts   # equity curve + drawdown
    assert tables >= 2, tables   # return per bulan + daftar trade
    print("OK  halaman /backtest jalan tanpa exception")


if __name__ == "__main__":
    main()
