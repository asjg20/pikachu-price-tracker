"""
Generate the Pikachu movers report: fetch the top movers, write docs/index.html
(for GitHub Pages), and email it if Gmail credentials are configured.

Run manually:   python generate_report.py
Run in CI:      invoked by .github/workflows/weekly-report.yml

Safe to run locally without any Gmail environment variables set -- it will
write docs/index.html and simply skip the email step, logging why.
"""

import logging
from pathlib import Path

from email_utils import is_email_configured, send_report_email
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

    if is_email_configured():
        send_report_email(html)
        logger.info("report emailed")
    else:
        logger.info(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD / REPORT_RECIPIENT not all set "
            "in the environment -- skipping email send (this is expected for "
            "a local run; see README.md / email_utils.py for setup)."
        )


if __name__ == "__main__":
    main()
