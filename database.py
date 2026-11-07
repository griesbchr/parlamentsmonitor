"""
SQLite database schema and helper functions for the Austrian Parliament Monitor.
"""
import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "parliament.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifesto_promises (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    party        TEXT NOT NULL,
    promise_text TEXT NOT NULL,
    policy_area  TEXT,
    promise_type TEXT,
    specificity  INTEGER DEFAULT 3,
    source_pages TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS promise_matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    promise_id      INTEGER NOT NULL REFERENCES manifesto_promises(id),
    item_id         INTEGER NOT NULL REFERENCES items(id),
    alignment       TEXT,
    relevance_score REAL,
    explanation     TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(promise_id, item_id)
);

CREATE TABLE IF NOT EXISTS items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    gp_code          TEXT NOT NULL,
    ityp             TEXT NOT NULL,
    inr              INTEGER NOT NULL,
    zitation         TEXT,
    title            TEXT,
    description      TEXT,   -- Vollständiger Gesetzestitel
    shortinfo        TEXT,   -- HTML-Kurzinfo mit Bullet Points
    status_text      TEXT,   -- Beschlusstext (Dafür/Dagegen + BGBl-Link)
    datum            TEXT,
    einlangen        TEXT,
    status_nr        INTEGER,
    doktyp           TEXT,
    doktyp_lang      TEXT,
    his_url          TEXT,
    vhg              TEXT,
    raw_json         TEXT,
    fetched_at       TEXT,
    UNIQUE(gp_code, ityp, inr)
);

CREATE TABLE IF NOT EXISTS votes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        INTEGER NOT NULL REFERENCES items(id),
    club_code      TEXT NOT NULL,
    club_name      TEXT,
    in_favor       INTEGER,  -- 1=dafür, 0=dagegen, NULL=nicht abgestimmt
    fraction_count INTEGER,
    UNIQUE(item_id, club_code)
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id),
    source  TEXT NOT NULL,  -- 'club', 'topic', 'headword', 'eurovoc'
    value   TEXT NOT NULL,
    PRIMARY KEY (item_id, source, value)
);

CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id),
    title      TEXT,       -- z.B. "Gesetzestext", "Erläuterungen"
    url        TEXT NOT NULL,
    text       TEXT,       -- extrahierter Plaintext
    html       TEXT,       -- Original-HTML vom Parlament
    fetched_at TEXT,
    UNIQUE(item_id, url)
);

CREATE TABLE IF NOT EXISTS manifesto_sections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    party        TEXT NOT NULL,
    section_path TEXT,          -- e.g. "Wirtschaft > Steuerpolitik"
    policy_area  TEXT,          -- e.g. Wirtschaft, Klimaschutz, Migration, ...
    text         TEXT NOT NULL, -- exact original text from the manifesto
    char_count   INTEGER,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vote_analyses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES items(id),
    party        TEXT NOT NULL,
    party_vote   TEXT,          -- JA / NEIN / ENTHALTUNG
    section_id   INTEGER REFERENCES manifesto_sections(id),
    score        INTEGER,       -- -2 contradiction .. 0 neutral .. +2 alignment
    explanation  TEXT,          -- Claude's reasoning in German
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(item_id, party)
);

-- Embedding vectors of manifesto sections (float32, L2-normalised; see embeddings.py)
CREATE TABLE IF NOT EXISTS section_embeddings (
    section_id  INTEGER PRIMARY KEY REFERENCES manifesto_sections(id) ON DELETE CASCADE,
    vector      BLOB NOT NULL
);

-- Parlamentskorrespondenz articles linked to an item (only the section about that item)
CREATE TABLE IF NOT EXISTS pk_articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    url         TEXT NOT NULL,      -- e.g. /aktuelles/pk/jahr_2025/pk0544#XXVIII_I_00069
    title       TEXT,
    date        TEXT,               -- DD.MM.YYYY
    text        TEXT,               -- plain text of the item's section
    fetched_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(item_id, url)
);

-- Each party's stated reasons for its vote, extracted by Claude from pk_articles
CREATE TABLE IF NOT EXISTS party_statements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    party       TEXT NOT NULL,
    statement   TEXT,               -- 2-3 sentences in German; NULL = nothing found
    speakers    TEXT,               -- JSON list of speaker names
    sources     TEXT,               -- JSON list of {title, url, date}
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(item_id, party)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    # wait up to 60 s for another process's write lock instead of failing after 5 s
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    # columns added after the first release
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vote_analyses)")}
    if "checked" not in cols:
        # NULL = not double-checked, 1 = contradiction confirmed, 0 = withdrawn on re-check
        conn.execute("ALTER TABLE vote_analyses ADD COLUMN checked INTEGER")
    conn.commit()


def upsert_item(conn: sqlite3.Connection, fields: dict) -> int:
    conn.execute(
        """
        INSERT INTO items
            (gp_code, ityp, inr, zitation, title, datum, einlangen,
             status_nr, doktyp, doktyp_lang, his_url, vhg)
        VALUES
            (:gp_code, :ityp, :inr, :zitation, :title, :datum, :einlangen,
             :status_nr, :doktyp, :doktyp_lang, :his_url, :vhg)
        ON CONFLICT(gp_code, ityp, inr) DO UPDATE SET
            zitation    = excluded.zitation,
            title       = excluded.title,
            datum       = excluded.datum,
            status_nr   = excluded.status_nr,
            his_url     = excluded.his_url
        """,
        fields,
    )
    row = conn.execute(
        "SELECT id FROM items WHERE gp_code=:gp_code AND ityp=:ityp AND inr=:inr",
        fields,
    ).fetchone()
    return row["id"]


def upsert_tags(conn: sqlite3.Connection, item_id: int, source: str, values: list[str]):
    conn.executemany(
        "INSERT OR IGNORE INTO item_tags (item_id, source, value) VALUES (?, ?, ?)",
        [(item_id, source, v) for v in values],
    )


def upsert_votes(conn: sqlite3.Connection, item_id: int, vote_result: list[dict]):
    conn.executemany(
        """
        INSERT INTO votes (item_id, club_code, club_name, in_favor, fraction_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id, club_code) DO UPDATE SET
            in_favor       = excluded.in_favor,
            fraction_count = excluded.fraction_count
        """,
        [
            (
                item_id,
                club.get("code"),
                club.get("text"),
                1 if club.get("infavor") else 0,
                club.get("fraction"),
            )
            for club in vote_result
        ],
    )


def extract_document_urls(content: dict) -> list[tuple[str, str]]:
    """Return [(title, html_url), ...] for all HTML documents in a detail page."""
    result = []
    for group in content.get("documents") or []:
        title = group.get("title", "")
        for doc in group.get("documents") or []:
            if doc.get("type") == "HTML":
                result.append((title, doc["link"]))
    return result


def upsert_document(conn: sqlite3.Connection, item_id: int, title: str, url: str, text: str, html: str):
    from datetime import datetime, timezone
    conn.execute(
        """
        INSERT INTO documents (item_id, title, url, text, html, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id, url) DO UPDATE SET
            text=excluded.text, html=excluded.html, fetched_at=excluded.fetched_at
        """,
        (item_id, title, url, text, html, datetime.now(timezone.utc).isoformat()),
    )


def extract_shortinfo(shortinfo: dict | None) -> str | None:
    if not shortinfo:
        return None
    parts = [shortinfo.get("teil1") or "", shortinfo.get("teil2") or ""]
    return "\n".join(p for p in parts if p).strip() or None


def store_detail(conn: sqlite3.Connection, item_id: int, raw: str, content: dict, vote_result: list[dict]):
    from datetime import datetime, timezone

    description  = content.get("description")
    shortinfo    = extract_shortinfo(content.get("shortinfo"))
    status_text  = (content.get("status") or {}).get("description")

    conn.execute(
        """UPDATE items
           SET raw_json    = ?,
               fetched_at  = ?,
               description = ?,
               shortinfo   = CASE WHEN shortinfo_llm = 1 THEN shortinfo ELSE ? END,
               status_text = ?
           WHERE id=?""",
        (raw, datetime.now(timezone.utc).isoformat(), description, shortinfo, status_text, item_id),
    )
    upsert_votes(conn, item_id, vote_result)
