"""
index_manifestos.py – Parse cleaned manifesto text files into sections
and store them in the manifesto_sections table.

Each section = one heading block + its body text.
Large sections (> MAX_CHUNK_CHARS) are split at blank-line boundaries
so no single DB row exceeds ~2 000 characters.

The exact original text is preserved in every row — never paraphrased.

Usage
-----
    python index_manifestos.py              # all parties
    python index_manifestos.py FPÖ NEOS    # specific parties
    python index_manifestos.py --reset ÖVP # delete + re-index a party
"""

import re
import sys
from pathlib import Path

import database as db

BASE_DIR      = Path(__file__).parent
MANIFESTO_DIR = BASE_DIR / "manifestos"

PARTIES = {
    "ÖVP":   "oevp_clean.txt",
    "SPÖ":   "spoe_clean.txt",
    "FPÖ":   "fpoe_clean.txt",
    "GRÜNE": "gruene_clean.txt",
    "NEOS":  "neos_clean.txt",
}

MAX_CHUNK_CHARS = 2000   # soft max; split at paragraph boundaries if exceeded
MIN_BODY_CHARS  = 60     # skip near-empty sections


# ── Policy area keyword mapping (German, lowercase) ──────────────────────────

POLICY_AREAS: dict[str, list[str]] = {
    "Wirtschaft": [
        "wirtschaft", "steuer", "finanzen", "budget", "unternehm", "körperschaft",
        "lohnnebenkosten", "konjunktur", "standort", "investition", "wettbewerb",
        "export", "handel", "entlastung", "sanierung", "schulden", "inflation",
        "kaufkraft", "wirtschaftswachstum", "bip",
    ],
    "Arbeit": [
        "arbeit", "lohn", "gehalt", "mindestlohn", "arbeitslosigkeit",
        "beschäftigung", "vollzeitarbeit", "teilzeit", "gewerkschaft",
        "arbeitsmarkt", "beruf", "fachkräfte",
    ],
    "Klimaschutz": [
        "klima", "umwelt", "energie", "erneuerbar", "co2", "emission",
        "nachhaltig", "natur", "ökologie", "wäld", "artenvielfalt",
        "mobilitätswende", "öffis", "öffentlicher verkehr", "energiewende",
        "kreislaufwirtschaft", "bodenschutz",
    ],
    "Verkehr": [
        "verkehr", "mobilität", "straße", "autobahn", "bahn", "öpnv",
        "radfahren", "fußgänger", "transit", "flughafen",
    ],
    "Soziales": [
        "sozial", "pension", "rente", "pflege", "armut", "sozialhilfe",
        "mindestsicherung", "inklusion", "behinder", "altern", "sozialstaat",
        "sozialsystem", "grundsicherung",
    ],
    "Bildung": [
        "bildung", "schule", "universität", "hochschule", "kindergarten",
        "lehrer", "studium", "ausbildung", "lehre", "erziehung", "pädagog",
        "lehrplan", "schulpflicht", "bildungsreform",
    ],
    "Gesundheit": [
        "gesundheit", "krankenhaus", "arzt", "ärztin", "medizin",
        "krankenversicherung", "pharmaz", "impf", "covid", "corona",
        "kassenstelle", "spital", "gesundheitsversorgung",
    ],
    "Sicherheit": [
        "sicherheit", "polizei", "kriminalität", "terror", "bundesheer",
        "heer", "verteidigung", "innere sicherheit", "exekutiv", "strafrecht",
    ],
    "Wohnen": [
        "wohnen", "wohnung", "miete", "immobilien", "bauen", "wohnbau",
        "baugenehmigung", "bodenpreise", "eigentum",
    ],
    "Demokratie": [
        "demokratie", "verfassung", "parlament", "volksbegehren",
        "volksabstimmung", "bürgerrecht", "transparenz", "korruption",
        "rechtsstaat", "justiz", "medien", "orf", "pressefreiheit",
        "wahlrecht", "wahlkampf",
    ],
    "Außenpolitik": [
        "außenpolitik", "europa", "europäisch", "eu-", " eu ", "nato",
        "neutralität", "international", "ukraine", "außen", "frieden",
        "geopolitik", "entwicklungshilfe",
    ],
    "Migration": [
        "migration", "asyl", "zuwander", "flucht", "integration",
        "staatsbürger", "ausländer", "grenze", "abschiebung",
        "aufenthaltsrecht", "einwanderung",
    ],
    "Digitalisierung": [
        "digitalisierung", "digitale", "digitalen", "digitaler",
        "internet", "technologie", "künstliche intelligenz", "ki-",
        "start-up", "it-infrastruktur", "informationstechnologie",
        "datenschutz", "cybersicherheit", "plattformökonomie",
        "e-government", "open data",
    ],
    "Landwirtschaft": [
        "landwirtschaft", "bauer", "bäuerin", "lebensmittel", "tierschutz",
        "tier", "wolf", "agrarpolitik", "ländlicher raum", "wald",
    ],
    "Familie": [
        "familie", "kinder", "eltern", "jugend", "geburt",
        "kinderbetreuung", "frauenpolitik", "gleichstellung", "frauen",
        "gender", "mutterschaft", "vaterschaft",
    ],
}


def detect_policy_area(heading: str, body: str) -> str:
    """Score each policy area by keyword hits in heading + first 600 chars of body."""
    combined = (heading + " " + body[:600]).lower()
    scores: dict[str, int] = {}
    for area, keywords in POLICY_AREAS.items():
        score = sum(combined.count(kw) for kw in keywords)
        if score:
            scores[area] = score
    if scores:
        return max(scores, key=scores.__getitem__)
    return "Sonstiges"


# ── Headings ──────────────────────────────────────────────────────────────────
# extract_manifestos.py marks headings explicitly: "# Kapitel", "## Abschnitt".

_HEADING_RE = re.compile(r'^(#{1,2})\s+(.+)$')


def generic_headings(lines: list[str], min_count: int = 5) -> set[str]:
    """
    Labels repeated in every chapter (GRÜNE: "UNSERE VISION", "SO SCHAUT'S AUS").
    They are kept as a third level under the real heading instead of replacing it.
    """
    counts: dict[str, int] = {}
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            counts[m.group(2).strip()] = counts.get(m.group(2).strip(), 0) + 1
    return {h for h, n in counts.items() if n >= min_count}


# ── Section splitting ─────────────────────────────────────────────────────────

def split_into_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split at blank-line boundaries if text exceeds max_chars."""
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r'\n{2,}', text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) > max_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [text]


# ── Core parser ───────────────────────────────────────────────────────────────

def parse_and_store(conn, party: str, text: str) -> int:
    """Parse clean text into sections, insert into manifesto_sections. Returns count."""
    lines = text.splitlines()
    h1 = ""
    h2 = ""
    current_heading  = "Einleitung"
    current_body:  list[str] = []
    rows_inserted = 0

    # Keywords that indicate imprint / legal boilerplate — skip those sections
    _IMPRINT_MARKERS = {"druckerei", "medieninhaberin", "grafik:", "hersteller:",
                        "isbn", "impressum", "offenlegung", "verlags"}

    def is_imprint(body: str) -> bool:
        sample = body[:300].lower()
        return sum(1 for m in _IMPRINT_MARKERS if m in sample) >= 2

    def flush(heading: str, body_lines: list[str]) -> None:
        nonlocal rows_inserted
        body = "\n".join(body_lines).strip()
        if len(body) < MIN_BODY_CHARS:
            return
        if is_imprint(body):
            return
        for chunk in split_into_chunks(body):
            if not chunk.strip():
                continue
            policy = detect_policy_area(heading, chunk)
            conn.execute(
                """
                INSERT INTO manifesto_sections
                    (party, section_path, policy_area, text, char_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (party, heading, policy, chunk, len(chunk)),
            )
            rows_inserted += 1

    generic = generic_headings(lines)
    h3 = ""

    def path() -> str:
        return " > ".join(h for h in (h1, h2, h3) if h) or "Einleitung"

    for line in lines:
        m = _HEADING_RE.match(line)
        if not m:
            current_body.append(line)
            continue
        flush(current_heading, current_body)
        current_body = []
        level, title = len(m.group(1)), m.group(2).strip()
        if title in generic:
            h3 = title
        elif level == 1:
            if title != h1:          # same chapter title repeated on the next page
                h1, h2 = title, ""
            h3 = ""
        else:
            h2, h3 = title, ""
        current_heading = path()

    flush(current_heading, current_body)
    conn.commit()
    return rows_inserted


# ── Entry point ───────────────────────────────────────────────────────────────

def run(parties_to_run: list[str], reset: bool = False) -> None:
    conn = db.connect()
    db.init_schema(conn)   # ensures all tables (including the new ones) exist

    for party in parties_to_run:
        if party not in PARTIES:
            print(f"Unknown party '{party}'. Valid: {list(PARTIES)}")
            continue

        src = MANIFESTO_DIR / PARTIES[party]
        if not src.exists():
            print(f"[{party}] Clean file not found: {src}")
            continue

        if reset:
            conn.execute("DELETE FROM manifesto_sections WHERE party=?", (party,))
            conn.commit()
            print(f"[{party}] Deleted existing sections.")

        existing = conn.execute(
            "SELECT COUNT(*) FROM manifesto_sections WHERE party=?", (party,)
        ).fetchone()[0]
        if existing > 0:
            print(f"[{party}] Already indexed ({existing} sections). Use --reset to re-index.")
            continue

        text = src.read_text(encoding="utf-8")
        n    = parse_and_store(conn, party, text)
        print(f"[{party}] {n} sections stored.")

    # Summary
    print("\n=== manifesto_sections summary ===")
    rows = conn.execute("""
        SELECT party,
               COUNT(*)            AS n,
               SUM(char_count)/1000 AS total_kchars,
               COUNT(DISTINCT policy_area) AS areas
        FROM   manifesto_sections
        GROUP  BY party
        ORDER  BY party
    """).fetchall()
    for r in rows:
        print(f"  {r['party']:<8}  {r['n']:>4} sections  "
              f"{r['total_kchars']:>4}k chars  "
              f"{r['areas']} policy areas")

    print("\n=== policy area distribution ===")
    rows2 = conn.execute("""
        SELECT policy_area, COUNT(*) AS n
        FROM   manifesto_sections
        GROUP  BY policy_area
        ORDER  BY n DESC
    """).fetchall()
    for r in rows2:
        print(f"  {r['policy_area']:<20}  {r['n']:>4}")

    conn.close()


if __name__ == "__main__":
    args   = sys.argv[1:]
    reset  = "--reset" in args
    args   = [a for a in args if a != "--reset"]
    parties = args if args else list(PARTIES.keys())
    run(parties, reset=reset)
