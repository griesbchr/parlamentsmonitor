"""
extract_promises.py – Extract concrete election promises from party manifestos.

Strategy
--------
1. Read each PDF in manifestos/ using pypdf.
2. Send the full text in chunks to Claude (haiku) with a structured extraction prompt.
3. Claude returns a JSON list of promises, each with:
   - promise_text (German)
   - policy_area  (e.g. Klimaschutz, Soziales, Wirtschaft)
   - promise_type (Vorhaben / Ziel / Forderung)
   - specificity  (1=vague … 5=concrete & measurable)
4. Store in manifesto_promises table (skips if party already fully extracted).

Usage
-----
    python extract_promises.py              # all parties
    python extract_promises.py ÖVP SPÖ     # specific parties
    python extract_promises.py --reset ÖVP # delete existing and re-extract
"""

import json
import re
import sys
import logging
from pathlib import Path

import pypdf
import anthropic

import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR  = Path(__file__).parent
MANIFESTO_DIR = BASE_DIR / "manifestos"

PARTIES = {
    "ÖVP":   "oevp.pdf",
    "SPÖ":   "spoe.pdf",
    "FPÖ":   "fpoe.pdf",
    "GRÜNE": "gruene.pdf",
    "NEOS":  "neos.pdf",
}

# Pages to skip (cover, table of contents, etc.)
SKIP_PAGES: dict[str, set[int]] = {
    "ÖVP":   {0, 1},
    "SPÖ":   {0},
    "FPÖ":   {0, 1},
    "GRÜNE": {0, 1},
    "NEOS":  {0},
}

CHUNK_SIZE = 15   # pages per Claude call
MODEL      = "claude-haiku-5-5"

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifesto_promises (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    party        TEXT NOT NULL,
    promise_text TEXT NOT NULL,
    policy_area  TEXT,
    promise_type TEXT,   -- Vorhaben / Ziel / Forderung
    specificity  INTEGER DEFAULT 3,  -- 1=vage … 5=messbar
    source_pages TEXT,   -- "12-14" (page range in the PDF)
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS promise_matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    promise_id      INTEGER NOT NULL REFERENCES manifesto_promises(id),
    item_id         INTEGER NOT NULL REFERENCES items(id),
    alignment       TEXT,   -- supports / contradicts / neutral
    relevance_score REAL,   -- 0.0-1.0
    explanation     TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(promise_id, item_id)
);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def extract_text_from_pdf(pdf_path: Path) -> list[tuple[int, str]]:
    """Return list of (page_number_1based, text) for non-empty pages."""
    pages = []
    reader = pypdf.PdfReader(str(pdf_path))
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            pages.append((i + 1, text))
    return pages


EXTRACTION_PROMPT = """\
Du analysierst das Wahlprogramm der Partei {party} zur österreichischen Nationalratswahl 2024.

Folgender Text stammt aus den Seiten {pages} des Wahlprogramms:

---
{text}
---

Extrahiere alle **konkreten Wahlversprechen und politischen Vorhaben** aus diesem Abschnitt.

Kriterien:
- Nur konkrete, überprüfbare Aussagen (z.B. „Wir werden X einführen", „Wir wollen Y abschaffen", „Unser Ziel ist Z%")
- Keine allgemeinen Wertaussagen oder Beschreibungen des Status quo
- Jedes Versprechen als eigenständiger Punkt
- Kurz und präzise (1-2 Sätze)

Antworte NUR mit validem JSON in folgendem Format:
[
  {{
    "promise_text": "Vollständiger Text des Versprechens auf Deutsch",
    "policy_area": "Themenbereich (z.B. Klimaschutz, Wirtschaft, Soziales, Bildung, Sicherheit, Gesundheit, Wohnen, Migration, Demokratie, Außenpolitik, Sonstiges)",
    "promise_type": "Vorhaben|Ziel|Forderung",
    "specificity": 1
  }},
  ...
]

Specificität-Skala: 1=sehr vage, 2=vage, 3=mittel, 4=konkret, 5=sehr konkret & messbar

Wenn kein Versprechen im Text vorkommt, antworte mit: []
"""


def extract_promises_from_chunk(
    client: anthropic.Anthropic,
    party: str,
    pages: list[tuple[int, str]],
) -> list[dict]:
    """Send one chunk of pages to Claude and return parsed promises."""
    page_nums = f"{pages[0][0]}–{pages[-1][0]}"
    combined = "\n\n".join(f"[Seite {n}]\n{t}" for n, t in pages)

    prompt = EXTRACTION_PROMPT.format(
        party=party,
        pages=page_nums,
        text=combined[:12000],  # safety cap
    )

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text").strip()

        # Extract JSON from possible markdown code blocks
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not json_match:
            return []
        parsed = json.loads(json_match.group())
        if not isinstance(parsed, list):
            return []

        for p in parsed:
            p["source_pages"] = page_nums

        return parsed
    except Exception as e:
        log.warning(f"  Chunk {page_nums} failed: {e}")
        return []


def process_party(
    client: anthropic.Anthropic,
    conn,
    party: str,
    pdf_path: Path,
    reset: bool = False,
):
    if reset:
        conn.execute("DELETE FROM manifesto_promises WHERE party=?", (party,))
        conn.commit()
        log.info(f"[{party}] Deleted existing promises")

    existing = conn.execute(
        "SELECT COUNT(*) FROM manifesto_promises WHERE party=?", (party,)
    ).fetchone()[0]
    if existing > 0:
        log.info(f"[{party}] Already has {existing} promises – skipping (use --reset to re-extract)")
        return

    log.info(f"[{party}] Reading {pdf_path.name} …")
    all_pages = extract_text_from_pdf(pdf_path)
    skip = SKIP_PAGES.get(party, set())
    pages = [(n, t) for n, t in all_pages if n not in skip]
    log.info(f"[{party}] {len(pages)} content pages")

    all_promises: list[dict] = []
    for i in range(0, len(pages), CHUNK_SIZE):
        chunk = pages[i : i + CHUNK_SIZE]
        log.info(f"[{party}] Chunk {i//CHUNK_SIZE+1}/{(len(pages)-1)//CHUNK_SIZE+1} "
                 f"(pages {chunk[0][0]}–{chunk[-1][0]}) …")
        promises = extract_promises_from_chunk(client, party, chunk)
        all_promises.extend(promises)
        log.info(f"[{party}]   → {len(promises)} promises extracted")

    log.info(f"[{party}] Storing {len(all_promises)} promises in DB …")
    conn.executemany(
        """
        INSERT INTO manifesto_promises
            (party, promise_text, policy_area, promise_type, specificity, source_pages)
        VALUES
            (:party, :promise_text, :policy_area, :promise_type, :specificity, :source_pages)
        """,
        [
            {
                "party":        party,
                "promise_text": p.get("promise_text", ""),
                "policy_area":  p.get("policy_area", "Sonstiges"),
                "promise_type": p.get("promise_type", "Vorhaben"),
                "specificity":  int(p.get("specificity", 3)),
                "source_pages": p.get("source_pages", ""),
            }
            for p in all_promises
            if p.get("promise_text")
        ],
    )
    conn.commit()
    log.info(f"[{party}] Done. {conn.execute('SELECT COUNT(*) FROM manifesto_promises WHERE party=?', (party,)).fetchone()[0]} promises stored.")


def run(parties_to_run: list[str], reset: bool = False):
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY nicht gesetzt.")
        print("Setze die Variable mit:  set ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    conn   = db.connect()
    ensure_schema(conn)

    for party in parties_to_run:
        if party not in PARTIES:
            log.warning(f"Unknown party: {party}. Valid: {list(PARTIES)}")
            continue
        pdf_path = MANIFESTO_DIR / PARTIES[party]
        if not pdf_path.exists():
            log.error(f"Missing PDF: {pdf_path}")
            continue
        process_party(client, conn, party, pdf_path, reset=reset)

    # Summary
    print("\n=== Extraktions-Zusammenfassung ===")
    rows = conn.execute("""
        SELECT party, COUNT(*) as n,
               ROUND(AVG(specificity), 1) as avg_spec
        FROM manifesto_promises
        GROUP BY party ORDER BY party
    """).fetchall()
    for r in rows:
        print(f"  {r['party']:<8}  {r['n']:>4} Versprechen  Avg. Spezifitaet: {r['avg_spec']}")

    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    reset = "--reset" in args
    args  = [a for a in args if a != "--reset"]

    parties = args if args else list(PARTIES.keys())
    run(parties, reset=reset)
