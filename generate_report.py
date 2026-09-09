"""
Generate the Pikachu movers report: fetch the top movers and write
docs/index.html for GitHub Pages.

Run manually:   python generate_report.py
Run in CI:      invoked by .github/workflows/weekly-report.yml
"""

import logging
from pathlib import Path

from pikachu_core import get_top_movers, render_html_report

DOCS_DIR = Path(__file__).parent / "docs"
OUTPUT_FILE = DOCS_DIR / "index.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    logger.info("fetching top Pikachu price movers...")
    movers = get_top_movers(n=10)

    html = render_html_report(movers)

    DOCS_DIR.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    logger.info("wrote %s", OUTPUT_FILE)


if __name__ == "__main__":
    main()
