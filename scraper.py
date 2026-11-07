"""
Austrian Parliament Monitor - Data Collection Pipeline

Fetches Verhandlungsgegenstände and their club-level voting records
from the Austrian Parliament Open Data API (CC BY 4.0).

Usage:
    python scraper.py                              # XXVIII GP, full run
    python scraper.py --gp XXVII                   # previous GP
    python scraper.py --von 01.05.2026 --bis 31.05.2026   # date range
    python scraper.py --details-only               # only fetch missing detail pages (DB items)
"""
import argparse
import json
import logging
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL = "https://www.parlament.gv.at"
LIST_API = f"{BASE_URL}/Filter/api/filter/data/101"
RATE_LIMIT = 0.25  # seconds between requests

# Column indices in the row arrays returned by the list API
COL = {
    "gp_code":    0,
    "ityp":       1,
    "inr":        2,
    "datum":      4,
    "art":        5,
    "title":      6,
    "zitation":   7,
    "status_nr":  10,
    "doktyp":     11,
    "doktyp_lang": 13,
    "his_url":    14,
    "einlangen":  16,
    "vhg":        17,
    "clubs":      21,
    "topics":     22,
    "headwords":  23,
    "eurovoc":    24,
}

# Minimum status that indicates a plenary vote occurred.
# 4 = Plenarberatungen NR, 5 = Beschlossen/abgelehnt.
VOTE_STATUS_THRESHOLD = 4

# Document types that carry club-level vote data.
# Note: the API does prefix-matching, so "A" also returns AB, A(E), etc.
# We filter exactly in Python.
# AA (Ausschussantrag) items store the vote in the `stages` field as plain
# text rather than in vote.result — handled by parse_stages_vote() below.
VOTE_DOKTYPEN = ["RV", "A", "AA", "BUA", "BRA", "RVS"]

# Club name → code/color used when parsing AA stages vote text.
# Covers all clubs present in GP XXVIII; extend here if new clubs form.
CLUB_MAP = {
    "FPÖ":   {"code": "F", "color": "#0052FB"},
    "ÖVP":   {"code": "V", "color": "#62C3D0"},
    "SPÖ":   {"code": "S", "color": "#FF0000"},
    "NEOS":  {"code": "N", "color": "#E3257B"},
    "GRÜNE": {"code": "G", "color": "#69B12E"},
    "Grüne": {"code": "G", "color": "#69B12E"},  # occasional variant
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "AustrianParliamentMonitor/1.0 (educational/research)"
    return s


def fetch_list_page(
    session: requests.Session,
    gp: str,
    page: int,
    datum_von: str | None = None,
    datum_bis: str | None = None,
    doktypen: list[str] | None = None,
) -> dict:
    body: dict = {
        "NRBR": ["NR"],
        "GP": [gp],
        # Server-side: only items that reached plenary vote stage
        "STATUS": [str(s) for s in range(VOTE_STATUS_THRESHOLD, 10)],
    }
    if datum_von:
        body["DATUM_VON"] = datum_von
    if datum_bis:
        body["DATUM_BIS"] = datum_bis
    if doktypen:
        body["R_DOKTYP"] = doktypen
    resp = session.post(LIST_API, params={"page": page}, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_detail(session: requests.Session, his_url: str) -> dict | None:
    url = f"{BASE_URL}{his_url}?json=TRUE"
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        log.debug(f"HTTP {e.response.status_code} for {url}")
        return None
    except Exception as e:
        log.warning(f"Error fetching {url}: {e}")
        return None


def parse_json_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def safe_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def row_to_fields(row: list) -> dict:
    return {
        "gp_code":    row[COL["gp_code"]],
        "ityp":       row[COL["ityp"]],
        "inr":        safe_int(row[COL["inr"]]),
        "zitation":   row[COL["zitation"]],
        "title":      row[COL["title"]],
        "datum":      row[COL["datum"]],
        "einlangen":  row[COL["einlangen"]],
        "status_nr":  safe_int(row[COL["status_nr"]]),
        "doktyp":     row[COL["doktyp"]],
        "doktyp_lang": row[COL["doktyp_lang"]],
        "his_url":    row[COL["his_url"]],
        "vhg":        row[COL["vhg"]],
    }


def phase_list(
    session: requests.Session,
    gp: str,
    datum_von: str | None = None,
    datum_bis: str | None = None,
    doktypen: list[str] | None = None,
) -> list[tuple[dict, dict]]:
    """Phase 1: Download the item list into memory. Nothing is written to DB.
    Returns a list of (fields, tags) tuples for candidates that may have vote data.
    """
    date_label = f" [{datum_von or ''}–{datum_bis or ''}]" if (datum_von or datum_bis) else ""
    dok_label  = f" doktyp={doktypen}" if doktypen else ""
    log.info(f"Phase 1 – Fetching item list for GP {gp}{date_label}{dok_label}")

    first = fetch_list_page(session, gp, 1, datum_von, datum_bis, doktypen)
    total_pages = first["pages"]
    total_count = first["count"]
    log.info(f"  {total_count} items across {total_pages} pages")

    pending = []

    with tqdm(total=total_pages, desc="List pages", unit="page") as bar:
        for page in range(1, total_pages + 1):
            payload = (
                first
                if page == 1
                else fetch_list_page(session, gp, page, datum_von, datum_bis, doktypen)
            )

            for row in payload.get("rows", []):
                fields = row_to_fields(row)

                # Exact doktyp match — API does prefix matching so "A" also returns AB, A(E), etc.
                if fields["doktyp"] not in VOTE_DOKTYPEN:
                    continue

                # Redundant safety net (server-side STATUS filter already applied)
                if fields["status_nr"] is not None and fields["status_nr"] < VOTE_STATUS_THRESHOLD:
                    continue

                tags = {
                    "club":     parse_json_list(row[COL["clubs"]]),
                    "topic":    parse_json_list(row[COL["topics"]]),
                    "headword": parse_json_list(row[COL["headwords"]]),
                    "eurovoc":  parse_json_list(row[COL["eurovoc"]]),
                }
                pending.append((fields, tags))

            bar.update(1)
            if page < total_pages:
                time.sleep(RATE_LIMIT)

    log.info(f"Phase 1 complete. {len(pending)} candidates for detail fetch.")
    return pending


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def parse_stages_vote(stages: list) -> list:
    """Extract club-level vote from AA-style stages entries.

    AA items lack a structured vote.result field; instead the vote outcome
    is embedded in a stages entry as an HTML string, e.g.:
        'Antrag <b>angenommen</b><br>Dafür: ÖVP, SPÖ, NEOS, GRÜNE, dagegen: FPÖ'

    Returns a vote.result-compatible list (fraction=None for all entries),
    or an empty list if no parseable vote stage is found.
    """
    import re

    for stage in stages:
        text = html_to_text(stage.get("text", ""))
        lower = text.lower()
        if "angenommen" not in lower and "abgelehnt" not in lower:
            continue

        result = []
        # Split into "Dafür: …" and "dagegen: …" sections
        m_for     = re.search(r'daf[üu]r\s*:\s*(.+?)(?=dagegen\s*:|$)', text, re.IGNORECASE)
        m_against = re.search(r'dagegen\s*:\s*(.+?)$',                   text, re.IGNORECASE)

        for_clubs     = [c.strip() for c in m_for.group(1).split(',')     if c.strip()] if m_for     else []
        against_clubs = [c.strip() for c in m_against.group(1).split(',') if c.strip()] if m_against else []

        for name, infavor in [(c, True) for c in for_clubs] + [(c, False) for c in against_clubs]:
            info = CLUB_MAP.get(name)
            if info:
                result.append({
                    "text":     name,
                    "code":     info["code"],
                    "color":    info["color"],
                    "fraction": None,  # not available in stages text
                    "infavor":  infavor,
                })

        if result:
            return result

    return []


def phase_details(
    session: requests.Session,
    conn,
    pending: list[tuple[dict, dict]] | None = None,
):
    """Phase 2: Fetch detail pages and extract vote data.

    - Full run (pending provided): items from Phase 1 memory list.
      Only written to DB if vote data is found → DB stays clean at all times.
    - details-only mode (pending=None): items already in DB without raw_json.
    """
    if pending is not None:
        # ── Full run: work from in-memory list ──────────────────────────────
        log.info(f"Phase 2 – Fetching detail pages for {len(pending)} candidates")
        votes_found = 0

        with tqdm(pending, desc="Detail pages", unit="item") as bar:
            for fields, tags in bar:
                detail = fetch_detail(session, fields["his_url"])
                if detail:
                    content    = detail.get("content", {})
                    vote       = content.get("vote") or {}
                    vote_result = vote.get("result", [])
                    # AA items store the vote in stages text instead of vote.result
                    if not vote_result:
                        vote_result = parse_stages_vote(content.get("stages", []))

                    if vote_result:
                        # Only now persist to DB
                        item_id = db.upsert_item(conn, fields)
                        for source, values in tags.items():
                            db.upsert_tags(conn, item_id, source, values)
                        raw = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
                        db.store_detail(conn, item_id, raw, content, vote_result)
                        votes_found += 1

                conn.commit()
                time.sleep(RATE_LIMIT)
                bar.set_postfix(votes=votes_found)

    else:
        # ── details-only: process items already in DB without raw_json ──────
        db_pending = conn.execute("""
            SELECT id, his_url, zitation
            FROM items
            WHERE raw_json IS NULL
              AND his_url IS NOT NULL
              AND (status_nr IS NULL OR status_nr >= ?)
            ORDER BY id
        """, (VOTE_STATUS_THRESHOLD,)).fetchall()

        log.info(f"Phase 2 – Fetching detail pages for {len(db_pending)} DB items")
        votes_found = 0

        with tqdm(db_pending, desc="Detail pages", unit="item") as bar:
            for row in bar:
                detail = fetch_detail(session, row["his_url"])
                if detail:
                    content     = detail.get("content", {})
                    vote        = content.get("vote") or {}
                    vote_result = vote.get("result", [])
                    # AA items store the vote in stages text instead of vote.result
                    if not vote_result:
                        vote_result = parse_stages_vote(content.get("stages", []))
                    raw = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
                    db.store_detail(conn, row["id"], raw, content, vote_result)
                    if vote_result:
                        votes_found += 1

                conn.commit()
                time.sleep(RATE_LIMIT)
                bar.set_postfix(votes=votes_found)

    log.info(f"Phase 2 complete. Vote records found: {votes_found}")


def phase_documents(session: requests.Session, conn):
    """Phase 3: Fetch HTML document texts for items with raw_json."""
    items = conn.execute(
        "SELECT id, raw_json FROM items WHERE raw_json IS NOT NULL"
    ).fetchall()

    pending = []
    for item in items:
        content = json.loads(item["raw_json"]).get("content", {})
        for title, url in db.extract_document_urls(content):
            existing = conn.execute(
                "SELECT 1 FROM documents WHERE item_id=? AND url=?", (item["id"], url)
            ).fetchone()
            if not existing:
                pending.append((item["id"], title, url))

    log.info(f"Phase 3 – Fetching {len(pending)} document HTML files")

    with tqdm(pending, desc="Documents", unit="doc") as bar:
        for item_id, title, url in bar:
            full_url = f"{BASE_URL}{url}"
            try:
                resp = session.get(full_url, timeout=30)
                resp.raise_for_status()
                raw_html = resp.text
                text = html_to_text(raw_html)
                db.upsert_document(conn, item_id, title, url, text, raw_html)
                conn.commit()
            except Exception as e:
                log.debug(f"Fehler bei {url}: {e}")
            time.sleep(RATE_LIMIT)

    log.info("Phase 3 complete.")


def run(
    gp: str,
    details_only: bool,
    datum_von: str | None = None,
    datum_bis: str | None = None,
    doktypen: list[str] | None = None,
):
    session = make_session()
    conn = db.connect()
    db.init_schema(conn)

    pending = None
    if not details_only:
        # Phase 1 returns in-memory list — nothing written to DB yet
        pending = phase_list(session, gp, datum_von, datum_bis, doktypen)

    # Phase 2: only writes to DB when vote data is confirmed
    phase_details(session, conn, pending)

    # Phase 3: fetch documents for items now in DB
    phase_documents(session, conn)

    # Safety cleanup: remove any legacy items without vote data
    # (items written by older scraper versions or --details-only runs without votes)
    conn.execute("DELETE FROM item_tags WHERE item_id NOT IN (SELECT DISTINCT item_id FROM votes)")
    deleted = conn.execute(
        "DELETE FROM items WHERE id NOT IN (SELECT DISTINCT item_id FROM votes)"
    ).rowcount
    conn.commit()
    if deleted:
        log.info(f"Cleaned up {deleted} legacy items without vote data.")

    conn.close()
    log.info("Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Austrian Parliament Monitor – scrapes voting data into parliament.db"
    )
    parser.add_argument(
        "--gp",
        default="XXVIII",
        help="Gesetzgebungsperiode, z.B. XXVII oder XXVIII (default: XXVIII)",
    )
    parser.add_argument(
        "--details-only",
        action="store_true",
        help="Nur fehlende Detail-Seiten nachladen (für Items bereits in der DB)",
    )
    parser.add_argument(
        "--von",
        default=None,
        metavar="TT.MM.JJJJ",
        help="Datum von (z.B. 01.05.2026)",
    )
    parser.add_argument(
        "--bis",
        default=None,
        metavar="TT.MM.JJJJ",
        help="Datum bis (z.B. 31.05.2026)",
    )
    args = parser.parse_args()

    run(
        gp=args.gp,
        details_only=args.details_only,
        datum_von=args.von,
        datum_bis=args.bis,
        doktypen=VOTE_DOKTYPEN,
    )


if __name__ == "__main__":
    main()
