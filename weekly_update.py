"""
Weekly Parliament scraper – runs automatically via Windows Task Scheduler.

First run (no .last_scrape file): fetches all decisions since GP XXVIII start.
Subsequent runs: fetches only decisions since the last successful run.
"""
import subprocess
import sys
import os
from datetime import date, datetime
from pathlib import Path

# Always run from the script's own directory
ROOT = Path(__file__).parent
os.chdir(ROOT)

# Run the pipeline in the project's own virtual environment if it exists
# (.venv holds fastembed etc. without touching the global Python packages).
_VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable

# Start of GP XXVIII – used for the initial full load
GP_START = date(2024, 10, 24)

# File that tracks the last successful scrape date
SCRAPE_MARKER = ROOT / ".last_scrape"


def fmt(d: date) -> str:
    """Format a date as DD.MM.YYYY for the scraper CLI."""
    return d.strftime("%d.%m.%Y")


def get_last_scrape() -> date | None:
    """Return the date of the last successful scrape, or None if never run."""
    if SCRAPE_MARKER.exists():
        try:
            return datetime.strptime(SCRAPE_MARKER.read_text().strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def save_last_scrape(d: date):
    """Save the date of the last successful scrape."""
    SCRAPE_MARKER.write_text(d.strftime("%Y-%m-%d"))


def main():
    today = date.today()

    last_scrape = get_last_scrape()
    von = last_scrape if last_scrape else GP_START
    first_run = last_scrape is None

    print(f"=== Weekly Parliament Update – {today} ===")
    if first_run:
        print("First run – fetching full GP XXVIII history from start")
    else:
        print(f"Last successful scrape: {fmt(last_scrape)}")
    print(f"Fetching GP XXVIII from {fmt(von)} to {fmt(today)}\n")

    # 1. Run scraper
    result = subprocess.run(
        [PYTHON, "scraper.py", "--gp", "XXVIII", "--von", fmt(von), "--bis", fmt(today)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("ERROR: scraper.py failed", file=sys.stderr)
        sys.exit(1)

    # Scraper succeeded — persist today as the last successful scrape date
    save_last_scrape(today)

    # 2. Generate LLM summaries for new items
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not api_key and sys.platform == "win32":
        # Try reading from Windows User-level env (for Task Scheduler context)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                api_key, _ = winreg.QueryValueEx(key, "ANTHROPIC_API_KEY")
            os.environ["ANTHROPIC_API_KEY"] = api_key
        except (ImportError, FileNotFoundError, OSError):
            pass
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set – skipping summary generation.")
        sys.exit(0)

    result = subprocess.run(
        [PYTHON, "generate_summaries.py"],
        cwd=ROOT,
        env=os.environ,
    )
    if result.returncode != 0:
        print("ERROR: generate_summaries.py failed", file=sys.stderr)
        sys.exit(1)

    # 3. Fetch Parlamentskorrespondenz reports and extract each party's stated reasons
    result = subprocess.run([PYTHON, "fetch_statements.py"], cwd=ROOT, env=os.environ)
    if result.returncode != 0:
        print("ERROR: fetch_statements.py failed", file=sys.stderr)
        sys.exit(1)

    # 4. Check new votes against party manifestos (only unanalysed pairs; Batch API)
    result = subprocess.run(
        [PYTHON, "analyze_votes.py", "--batch"],
        cwd=ROOT,
        env=os.environ,
    )
    if result.returncode != 0:
        print("ERROR: analyze_votes.py failed", file=sys.stderr)
        sys.exit(1)

    # 5. Export the static site and push it to GitHub Pages (if site/ has a remote)
    result = subprocess.run([PYTHON, "export_static.py", "--push"], cwd=ROOT, env=os.environ)
    if result.returncode != 0:
        print("ERROR: export_static.py failed", file=sys.stderr)
        sys.exit(1)

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
