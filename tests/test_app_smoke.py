"""Smoke test app utama (streamlit_app.py): python3 tests/test_app_smoke.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest


def main():
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120)
    at.run()
    assert not at.exception, f"exception: {at.exception}"
    assert not at.error, [str(e.value) for e in at.error]
    # tabs utama harus ada
    print("OK  app utama jalan, exception=None, error=None")


if __name__ == "__main__":
    main()
