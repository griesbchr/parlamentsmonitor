"""
fetch_pk_scores.py – Count Parlamentskorrespondenz (PK) mentions per item.

Strategy
--------
1. Load all item his_urls from the DB.
2. Collect the unique voting dates from the DB.
3. Fetch all PK list entries for 2024–2026 in memory (~142 pages, ~2800 entries).
4. Keep only PKs published within ±3 days of a voting date (plenary coverage)
   plus any PK that explicitly links a GP XXVIII item (caught in step 6).
5. Fetch detail pages for those PKs.
6. Count /gegenstand/XXVIII/... links that match DB his_urls.
7. Write media_score back to the items table.
"""

import json
import re
import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from tqdm import tqdm

import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL    = "https://www.parlament.gv.at"
PK_LIST_API = f"{BASE_URL}/Filter/api/filter/data/110"
RATE_LIMIT  = 0.3   # seconds between requests
DATE_WINDOW = 3     # days around a voting date to consider a PK relevant


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "AustrianParliamentMonitor/1.0 (PK score fetcher)"
    return s


# ── Phase 1: fetch PK list ────────────────────────────────────────────────────

def fetch_all_pk_list(session: requests.Session) -> list[tuple[str, str | None, str | None]]:
    """
    Fetch all PK list entries for 2024–2026.
    Returns list of (pk_nr, date_yyyymmdd, url_path).
    """
    log.info("Phase 1 – Fetching PK list for 2024–2026 …")
    body = {"JAHR": ["2024", "2025", "2026"]}

    first = session.post(PK_LIST_API, json=body, timeout=30).json()
    pages = first.get("pages", 1)
    total = first.get("count", 0)
    log.info(f"  {total} PK entries across {pages} pages")

    def extract_rows(data):
        result = []
        for r in data.get("rows", []):
            pk_nr     = r[2]                       # "PK0460"
            date_str  = r[4][:8] if r[4] else None  # "20260522"
            url_path  = r[7]                       # "/aktuelles/pk/jahr_2026/pk0460"
            result.append((pk_nr, date_str, url_path))
        return result

    all_pks = extract_rows(first)

    with tqdm(range(2, pages + 1), desc="PK list pages", unit="page") as bar:
        for page in bar:
            resp = session.post(PK_LIST_API, params={"page": page}, json=body, timeout=30)
            resp.raise_for_status()
            all_pks.extend(extract_rows(resp.json()))
            time.sleep(RATE_LIMIT)

    log.info(f"  Fetched {len(all_pks)} PK entries total")
    return all_pks


# ── Phase 2: fetch detail pages ───────────────────────────────────────────────

def fetch_pk_links(session: requests.Session, url_path: str) -> set[str]:
    """
    Fetch one PK detail page and return the set of /gegenstand/XXVIII/... paths.
    """
    try:
        resp = session.get(f"{BASE_URL}{url_path}?json=TRUE", timeout=30)
        resp.raise_for_status()
        content = resp.json().get("content", [])
        links: set[str] = set()
        for block in content:
            text = json.dumps(block.get("data", {}))
            for link in re.findall(r"/gegenstand/XXVIII/[A-Z]+/\d+", text):
                links.add(link)
        return links
    except Exception as e:
        log.debug(f"  skip {url_path}: {e}")
        return set()


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    session = make_session()
    conn    = db.connect()

    # Ensure media_score column exists
    try:
        conn.execute("ALTER TABLE items ADD COLUMN media_score INTEGER DEFAULT 0")
        conn.commit()
        log.info("Added media_score column to items table")
    except Exception:
        pass  # already exists

    # Load items
    items     = conn.execute("SELECT id, his_url, datum, zitation FROM items").fetchall()
    url_to_id = {r["his_url"]: r["id"] for r in items if r["his_url"]}
    log.info(f"Loaded {len(url_to_id)} DB items")

    # Collect unique voting dates
    voting_dates: set[datetime] = set()
    for r in items:
        if not r["datum"]:
            continue
        try:
            d, m, y = r["datum"].split(".")
            voting_dates.add(datetime(int(y), int(m), int(d)))
        except Exception:
            pass
    log.info(f"Found {len(voting_dates)} unique voting dates in DB")

    # Fetch PK list
    all_pks = fetch_all_pk_list(session)

    # Filter to PKs near a voting date
    log.info(f"Phase 2 – Filtering PKs within ±{DATE_WINDOW} days of a voting date …")
    relevant: list[tuple[str, str]] = []
    for pk_nr, date_str, url_path in all_pks:
        if not date_str or not url_path:
            continue
        try:
            pk_date = datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            continue
        for vd in voting_dates:
            if abs((pk_date - vd).days) <= DATE_WINDOW:
                relevant.append((pk_nr, url_path))
                break

    log.info(f"  {len(relevant)} PKs selected for detail fetch")

    # Fetch detail pages and tally hits
    scores: dict[int, int] = defaultdict(int)
    with tqdm(relevant, desc="PK detail pages", unit="pk") as bar:
        for pk_nr, url_path in bar:
            links = fetch_pk_links(session, url_path)
            for link in links:
                if link in url_to_id:
                    scores[url_to_id[link]] += 1
            time.sleep(RATE_LIMIT)

    # Write scores to DB (zero-out items with no hits)
    log.info(f"Phase 3 – Writing media_score for {len(items)} items …")
    for r in items:
        conn.execute(
            "UPDATE items SET media_score = ? WHERE id = ?",
            (scores.get(r["id"], 0), r["id"]),
        )
    conn.commit()
    conn.close()

    # Summary
    log.info("Done. Top 15 by media coverage:")
    conn2 = db.connect()
    top = conn2.execute(
        "SELECT zitation, title, media_score FROM items ORDER BY media_score DESC LIMIT 15"
    ).fetchall()
    for r in top:
        print(f"  {r['media_score']:3d}  {r['zitation']:<14}  {r['title'][:70]}")


if __name__ == "__main__":
    run()
