"""
analyze_votes.py – For each parliament vote, assess whether each party's
voting behaviour contradicts their manifesto.

Method (vote-centric, two-phase)
---------------------------------
Phase 1 – Section retrieval  (no API call)
  For each (item, party) pair:
    a. Map vote tags + title to policy_area(s)
    b. Retrieve top-3 manifesto_sections for that party + area
    c. Fall back to keyword-overlap search across all sections

Phase 2 – Claude contradiction assessment
  Prompt: vote title/description + party vote direction + manifesto excerpts
  Response: score (-2…+2), most relevant section index, explanation in German

Results stored in vote_analyses:
  item_id, party, party_vote, section_id, score, explanation

Score scale
-----------
  -2  Clear contradiction  – party voted against an explicit manifesto position
  -1  Mild contradiction   – vote diverges from manifesto direction
   0  No relation          – vote topic not covered in manifesto
  +1  Consistent           – vote aligns with general manifesto direction
  +2  Direct fulfilment    – party voted for a concrete manifesto promise

Usage
-----
    python analyze_votes.py                    # all parties, all votes (sequential)
    python analyze_votes.py --party FPÖ SPÖ   # specific parties
    python analyze_votes.py --limit 50         # first N voted items
    python analyze_votes.py --reset            # delete existing and redo

    python analyze_votes.py --batch            # submit batch job (50 % discount)
    python analyze_votes.py --batch --party FPÖ --limit 50
    python analyze_votes.py --resume BATCH_ID  # poll + store results of a previous batch
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"

# File where the active batch ID is saved so --resume can pick it up automatically
BATCH_ID_FILE = Path(__file__).parent / "vote_analysis_batch.json"

# Nationalrat club codes → manifesto party name
CLUB_TO_PARTY: dict[str, str] = {
    "V": "ÖVP",
    "S": "SPÖ",
    "F": "FPÖ",
    "G": "GRÜNE",
    "N": "NEOS",
}

PARTIES = ["ÖVP", "SPÖ", "FPÖ", "GRÜNE", "NEOS"]


# ── Policy area keyword mapping (for tag/title → area lookup) ─────────────────

_AREA_KW: dict[str, list[str]] = {
    "Wirtschaft":     ["wirtschaft", "steuer", "finanzen", "budget", "unternehm",
                       "körperschaft", "gewerbe", "wettbewerb", "konjunktur"],
    "Arbeit":         ["arbeit", "beschäftigung", "lohn", "mindestlohn", "arbeitsmarkt",
                       "gewerkschaft", "kündigung", "arbeitslosigkeit"],
    "Klimaschutz":    ["klima", "umwelt", "energie", "erneuerbar", "co2", "emission",
                       "nachhalt", "ökologie", "energiewende"],
    "Verkehr":        ["verkehr", "mobilität", "straße", "autobahn", "bahn",
                       "transit", "öpnv", "flughafen"],
    "Soziales":       ["sozial", "pension", "pflege", "armut", "sozialhilfe",
                       "mindestsicherung", "inklusion", "sozialstaat"],
    "Bildung":        ["bildung", "schule", "universität", "hochschule",
                       "kindergarten", "lehre", "ausbildung"],
    "Gesundheit":     ["gesundheit", "krankenhaus", "arzt", "medizin",
                       "krankenversicherung", "impf", "covid"],
    "Sicherheit":     ["sicherheit", "polizei", "bundesheer", "verteidigung",
                       "kriminalität", "strafrecht", "terrorismus"],
    "Wohnen":         ["wohnen", "wohnung", "miete", "wohnbau", "immobilien",
                       "mietrecht", "wohnkosten"],
    "Demokratie":     ["demokratie", "verfassung", "volksbegehren", "transparenz",
                       "korruption", "rechtsstaat", "justiz", "medien", "orf"],
    "Außenpolitik":   ["europa", "europäisch", " eu ", "nato", "neutralität",
                       "außenpolitik", "international", "ukraine"],
    "Migration":      ["migration", "asyl", "zuwander", "integration",
                       "staatsbürger", "abschiebung", "grenzschutz"],
    "Digitalisierung":["digital", "digitalisierung", "internet", "technologie",
                       "ki", "künstliche intelligenz", "datenschutz"],
    "Landwirtschaft": ["landwirtschaft", "bauer", "lebensmittel", "tierschutz",
                       "agrarpolitik", "wolf"],
    "Familie":        ["familie", "kinder", "eltern", "frauen", "gleichstellung",
                       "kinderbetreuung", "gender"],
}


def text_to_areas(text: str, top_k: int = 2) -> list[str]:
    t = text.lower()
    scores = {area: sum(t.count(kw) for kw in kws)
              for area, kws in _AREA_KW.items()}
    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    return [a for a in ranked[:top_k] if scores[a] > 0]


def keyword_overlap(section_text: str, query: str) -> float:
    qw = set(re.findall(r'\w{4,}', query.lower()))
    if not qw:
        return 0.0
    tw = set(re.findall(r'\w{4,}', section_text.lower()))
    return len(qw & tw) / len(qw)


def get_document_text(conn, item_id: int, max_chars: int = 6000) -> tuple[str, str]:
    """
    Return (doc_title, truncated_text) for the most informative document of an item.
    Preference: Erläuterungen > Gesetzestext > Initiativantrag > any other with text.
    Returns ('', '') if nothing available.
    """
    _PREF = [
        "Erläuterungen",
        "Gesetzestext",
        "Initiativantrag (elektr. übermittelte Version)",
        "Initiativantrag (textinterpretierte Version)",
        "Berichterstattung",
        "Vorblatt",
    ]
    rows = conn.execute(
        "SELECT title, text FROM documents WHERE item_id=? AND text IS NOT NULL AND LENGTH(text) > 200",
        (item_id,),
    ).fetchall()
    if not rows:
        return ("", "")
    by_title = {r["title"]: r["text"] for r in rows}
    for pref in _PREF:
        if pref in by_title:
            return (pref, by_title[pref][:max_chars])
    # fallback: longest document
    best = max(rows, key=lambda r: len(r["text"] or ""))
    return (best["title"], (best["text"] or "")[:max_chars])


def get_party_statement(conn, item_id: int, party: str) -> str:
    """The party's stated reasons for its vote (from fetch_statements.py), or ''."""
    try:
        row = conn.execute(
            "SELECT statement FROM party_statements WHERE item_id=? AND party=?",
            (item_id, party),
        ).fetchone()
    except Exception:
        return ""
    return (row["statement"] or "") if row else ""


# How manifesto passages are found for a vote:
#   "embedding" – semantic similarity (embeddings.py; needs fastembed in .venv)
#   "keyword"   – policy-area keywords + word overlap (no extra dependencies)
RETRIEVAL = "embedding"
_qvec_cache: dict = {}


def _query_text(conn, item_id: int | None, title: str, description: str, tags: list[str]) -> str:
    """What the vote is about: title, description, keywords and the start of the law text."""
    parts = [title, description or "", ", ".join(tags)]
    if item_id is not None:
        _, doc = get_document_text(conn, item_id, max_chars=1200)
        if doc:
            parts.append(doc)
        else:
            row = conn.execute("SELECT shortinfo FROM items WHERE id=?", (item_id,)).fetchone()
            if row and row["shortinfo"]:
                parts.append(re.sub(r"<[^>]+>", " ", row["shortinfo"]))
    return "\n".join(p for p in parts if p)


def find_sections(conn, party: str, title: str, description: str,
                  tags: list[str], top_k: int = 3, item_id: int | None = None) -> list[dict]:
    """Top-k most relevant manifesto sections for this vote and party."""
    if RETRIEVAL == "embedding":
        import embeddings
        if embeddings.available():
            key = item_id if item_id is not None else title
            if key not in _qvec_cache:
                _qvec_cache[key] = embeddings.query_vector(
                    _query_text(conn, item_id, title, description, tags))
            hits = embeddings.top_sections(conn, party, _qvec_cache[key], top_k)
            if hits:
                by_id = {r["id"]: dict(r) for r in conn.execute(
                    f"SELECT id, section_path, policy_area, text, char_count FROM manifesto_sections "
                    f"WHERE id IN ({','.join('?' * len(hits))})", [h[0] for h in hits])}
                return [by_id[sid] for sid, _ in hits if sid in by_id]
    return find_sections_keyword(conn, party, title, description, tags, top_k)


def _prepare_retrieval(conn) -> None:
    """Make sure every manifesto section has an embedding (no-op for keyword retrieval)."""
    global RETRIEVAL
    if RETRIEVAL != "embedding":
        return
    import embeddings
    if not embeddings.available():
        log.warning("fastembed not installed – using keyword retrieval (run inside .venv)")
        RETRIEVAL = "keyword"
        return
    added = embeddings.ensure_section_embeddings(conn)
    if added:
        log.info(f"Embedded {added} new manifesto sections")


def find_sections_keyword(conn, party: str, title: str, description: str,
                          tags: list[str], top_k: int = 3) -> list[dict]:
    """
    Previous method: policy_area match → keyword overlap fallback.
    """
    query_text = f"{title} {description} {' '.join(tags)}"
    areas = text_to_areas(query_text)

    candidates: list[dict] = []

    if areas:
        placeholders = ",".join("?" * len(areas))
        rows = conn.execute(
            f"SELECT id, section_path, policy_area, text, char_count "
            f"FROM manifesto_sections "
            f"WHERE party=? AND policy_area IN ({placeholders}) "
            f"ORDER BY char_count DESC",
            [party, *areas],
        ).fetchall()
        candidates = [dict(r) for r in rows]

    # Augment with keyword overlap if we have too few
    if len(candidates) < top_k:
        all_rows = conn.execute(
            "SELECT id, section_path, policy_area, text, char_count "
            "FROM manifesto_sections WHERE party=?",
            (party,),
        ).fetchall()
        existing_ids = {c["id"] for c in candidates}
        scored = sorted(
            [(keyword_overlap(r["text"], query_text), dict(r))
             for r in all_rows if r["id"] not in existing_ids],
            key=lambda x: x[0], reverse=True,
        )
        for score, row in scored:
            if score <= 0:
                break
            candidates.append(row)
            if len(candidates) >= top_k * 2:
                break

    # Sort by char_count (prefer longer, more substantive excerpts) and take top_k
    candidates.sort(key=lambda r: r["char_count"], reverse=True)
    return candidates[:top_k]


# ── Claude prompt ─────────────────────────────────────────────────────────────

_PROMPT = """\
Du analysierst das Abstimmungsverhalten der Partei {party} im österreichischen Nationalrat.

## Abstimmung
Titel: {title}
Beschreibung: {description}
Abstimmungsergebnis der Partei {party}: **{vote_direction}**

## Begründung der Partei {party} (laut Parlamentskorrespondenz)
{statement}

## Gesetzestext / Erläuterungen (Auszug)
{doc_text}

## Relevante Passagen aus dem Wahlprogramm der Partei {party}
{sections_text}
## Aufgabe
Beurteile, ob das Abstimmungsverhalten ({vote_direction}) der Partei {party} mit ihrem \
Wahlprogramm übereinstimmt oder im Widerspruch dazu steht.

Berücksichtige die Begründung der Partei, falls vorhanden: Lehnt sie z. B. ein Gesetz \
wegen konkreter Einzelheiten ab, obwohl sie das grundsätzliche Ziel teilt, ist das kein \
klarer Widerspruch. Die Begründung ist aber Selbstdarstellung – prüfe, ob sie inhaltlich \
zum Wahlprogramm passt, und übernimm sie nicht ungeprüft.

Bewertungsskala:
  -2 = Klarer Widerspruch: Das Wahlprogramm enthält eine ausdrückliche Aussage genau zum \
Gegenstand dieser Abstimmung, und die Partei stimmte entgegengesetzt.
  -1 = Widerspruch: Das Wahlprogramm äußert sich konkret zum Gegenstand der Abstimmung, \
und das Abstimmungsverhalten weicht davon ab.
   0 = Kein Bezug oder unklar: Das Wahlprogramm behandelt den konkreten Gegenstand nicht \
(nur allgemeine Ziele), oder Inhalt bzw. Bezug der Abstimmung sind aus den Unterlagen nicht erkennbar.
  +1 = Konsistent mit der Richtung des Wahlprogramms
  +2 = Direkte Umsetzung eines konkreten Wahlversprechens

Wichtige Regeln:
- Ein Widerspruch (-1/-2) setzt eine KONKRETE Aussage des Wahlprogramms zum Gegenstand der \
Abstimmung voraus. Allgemeine Ziele (z. B. „leistbares Wohnen“, „Unterstützung für Familien“, \
„Bürokratieabbau“) reichen dafür nicht aus – dann 0 oder +1.
- Fehlen Informationen zum Inhalt (z. B. Abänderungs- oder Entschließungsantrag ohne Text), vergib 0.
- Bei Sammelgesetzen und Budgets: Eine Ablehnung wegen anderer Teile des Pakets ist kein \
Widerspruch, wenn die Begründung das nahelegt.
- Eine Abstimmung, die zur erkennbaren Linie des Programms passt (auch eine Ablehnung), ist kein \
Widerspruch.

Antworte NUR mit validem JSON (kein Markdown außerhalb):
{{
  "score": <ganze Zahl -2 bis 2, ohne Pluszeichen>,
  "relevant_section_index": <0-basierter Index der relevantesten Passage, oder null>,
  "explanation": "<1-2 Sätze auf Deutsch>"
}}"""


def build_prompt(party: str, vote_direction: str,
                 title: str, description: str,
                 sections: list[dict],
                 doc_title: str = "", doc_text: str = "",
                 statement: str = "") -> str:
    sections_text = ""
    for i, sec in enumerate(sections):
        excerpt = sec["text"][:1500]
        sections_text += f"[Passage {i} – {sec['section_path']}]\n{excerpt}\n\n"

    if doc_text:
        doc_block = f"[{doc_title}]\n{doc_text}"
        if len(doc_text) >= 5990:
            doc_block += "\n[…Auszug, gekürzt]"
    else:
        doc_block = "(kein Dokumenttext verfügbar)"

    return _PROMPT.format(
        party=party,
        title=title,
        description=(description or "")[:600],
        vote_direction=vote_direction,
        doc_text=doc_block,
        sections_text=sections_text.strip(),
        statement=statement or "(keine Begründung verfügbar)",
    )


def parse_claude_response(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    # Claude sometimes writes "score": +1, which is not valid JSON
    text = re.sub(r'(:\s*)\+(\d)', r'\1\2', m.group())
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: unescaped quotes inside the explanation (e.g. „…" closed with a
        # plain ") break strict JSON – pull the three fields out directly.
        s = re.search(r'"score"\s*:\s*(-?\d)', text)
        i = re.search(r'"relevant_section_index"\s*:\s*(\d+|null)', text)
        e = re.search(r'"explanation"\s*:\s*"(.*)"\s*\}?\s*$', text, re.DOTALL)
        if not (s and e):
            return None
        result = {
            "score": int(s.group(1)),
            "relevant_section_index": int(i.group(1)) if i and i.group(1) != "null" else None,
            "explanation": e.group(1).replace('\\"', '"').strip(),
        }
    try:
        result["score"] = max(-2, min(2, int(result.get("score", 0))))
    except (TypeError, ValueError):
        return None
    return result


def call_claude(client: anthropic.Anthropic, party: str, vote_direction: str,
                title: str, description: str,
                sections: list[dict],
                doc_title: str = "", doc_text: str = "",
                statement: str = "") -> dict | None:
    if not sections and not doc_text:
        return {"score": 0, "relevant_section_index": None,
                "explanation": "Keine relevanten Wahlprogramm-Passagen gefunden."}

    prompt = build_prompt(party, vote_direction, title, description, sections,
                          doc_title=doc_title, doc_text=doc_text, statement=statement)

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            thinking={"type": "disabled"},   # structured task: answer directly
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text").strip()
        result = parse_claude_response(raw)
        if result is None:
            log.warning(f"  No JSON in response: {raw[:100]}")
        return result
    except json.JSONDecodeError as e:
        log.warning(f"  JSON parse error: {e}")
        return None
    except Exception as e:
        log.warning(f"  API error: {e}")
        return None


def get_party_vote(conn, item_id: int, party: str) -> str | None:
    """Return 'JA', 'NEIN', 'ENTHALTUNG', or None if not voted."""
    codes = [c for c, p in CLUB_TO_PARTY.items() if p == party]
    for code in codes:
        row = conn.execute(
            "SELECT in_favor FROM votes WHERE item_id=? AND club_code=?",
            (item_id, code),
        ).fetchone()
        if row is not None:
            if row["in_favor"] == 1:
                return "JA"
            elif row["in_favor"] == 0:
                return "NEIN"
            else:
                return "ENTHALTUNG"
    return None


def store_result(conn, item_id: int, party: str, vote_dir: str,
                 sections: list[dict], result: dict) -> bool:
    """
    Write one analysis row. Returns True if it was a contradiction.
    sections is needed to resolve relevant_section_index → section_id.
    """
    score      = int(result.get("score", 0))
    expl       = result.get("explanation", "")
    idx        = result.get("relevant_section_index")
    section_id = (sections[idx]["id"]
                  if idx is not None and 0 <= idx < len(sections)
                  else None)

    conn.execute(
        "INSERT OR REPLACE INTO vote_analyses "
        "(item_id, party, party_vote, section_id, score, explanation) "
        "VALUES (?,?,?,?,?,?)",
        (item_id, party, vote_dir, section_id, score, expl),
    )
    conn.commit()
    return score <= -1


def print_summary(conn) -> None:
    print("\n=== Vote analysis summary ===")
    rows = conn.execute("""
        SELECT party,
               COUNT(*)                                    AS total,
               SUM(CASE WHEN score <= -1 THEN 1 ELSE 0 END) AS contra,
               SUM(CASE WHEN score = -2  THEN 1 ELSE 0 END) AS strong,
               SUM(CASE WHEN score >= 1  THEN 1 ELSE 0 END) AS aligned,
               ROUND(AVG(score), 2)                        AS avg
        FROM vote_analyses
        GROUP BY party ORDER BY contra DESC
    """).fetchall()
    for r in rows:
        pct = round(100 * r["contra"] / r["total"]) if r["total"] else 0
        print(f"  {r['party']:<8}  {r['total']:>4} analysed  "
              f"{r['contra']:>3} contradictions ({pct}%)  "
              f"{r['strong']:>3} strong  {r['aligned']:>3} aligned  "
              f"avg={r['avg']}")


# ── Sequential mode ───────────────────────────────────────────────────────────

def run(parties_to_run: list[str], limit: int | None = None,
        reset: bool = False) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    conn   = db.connect()
    db.init_schema(conn)
    _prepare_retrieval(conn)

    if reset:
        for party in parties_to_run:
            conn.execute("DELETE FROM vote_analyses WHERE party=?", (party,))
        conn.commit()
        log.info(f"Reset analyses for: {parties_to_run}")

    # All items that have at least one vote recorded
    q = ("SELECT DISTINCT i.id, i.title, i.description, i.datum "
         "FROM items i JOIN votes v ON v.item_id = i.id "
         "WHERE i.title IS NOT NULL ORDER BY i.datum DESC")
    if limit:
        q += f" LIMIT {limit}"
    items = conn.execute(q).fetchall()
    log.info(f"{len(items)} voted items  x  {len(parties_to_run)} parties  "
             f"= up to {len(items)*len(parties_to_run)} API calls")

    done = contradictions = 0

    for item in items:
        tags = [r["value"] for r in conn.execute(
            "SELECT value FROM item_tags "
            "WHERE item_id=? AND source IN ('headword','topic')",
            (item["id"],),
        ).fetchall()]

        doc_title, doc_text = get_document_text(conn, item["id"])

        for party in parties_to_run:
            # Skip already-analysed (allows resuming interrupted runs)
            if conn.execute(
                "SELECT 1 FROM vote_analyses WHERE item_id=? AND party=?",
                (item["id"], party),
            ).fetchone():
                continue

            vote_dir = get_party_vote(conn, item["id"], party)
            if not vote_dir:
                continue

            sections = find_sections(
                conn, party,
                title=item["title"] or "",
                description=item["description"] or "",
                tags=tags,
                item_id=item["id"],
            )

            result = call_claude(
                client, party, vote_dir,
                title=item["title"] or "",
                description=item["description"] or "",
                sections=sections,
                doc_title=doc_title,
                doc_text=doc_text,
                statement=get_party_statement(conn, item["id"], party),
            )
            if result is None:
                continue

            is_contra = store_result(conn, item["id"], party, vote_dir, sections, result)
            done += 1
            if is_contra:
                contradictions += 1
            score = int(result.get("score", 0))
            if score != 0:
                log.info(f"  [{party:5}] {score:+d}  {vote_dir:5}  "
                         f"\"{item['title'][:55]}\"")

    log.info(f"Done. {done} analyses written, {contradictions} contradictions.")
    print_summary(conn)
    conn.close()


# ── Batch mode ────────────────────────────────────────────────────────────────

def _encode_custom_id(item_id: int, party: str) -> str:
    """Encode (item_id, party) into a batch custom_id string."""
    # party names may contain non-ASCII (ÖVP, GRÜNE) – use safe encoding
    safe_party = party.encode("utf-8").hex()
    return f"vote_{item_id}_{safe_party}"


def _decode_custom_id(custom_id: str) -> tuple[int, str] | None:
    """Decode custom_id back to (item_id, party). Returns None on failure."""
    m = re.match(r'^vote_(\d+)_([0-9a-f]+)$', custom_id)
    if not m:
        return None
    item_id   = int(m.group(1))
    party     = bytes.fromhex(m.group(2)).decode("utf-8")
    return item_id, party


def run_batch(parties_to_run: list[str], limit: int | None = None,
              reset: bool = False) -> None:
    """
    Build all prompts, submit them as a single batch job, poll until done,
    then store results. Saves the batch ID so --resume can recover it.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    conn   = db.connect()
    db.init_schema(conn)
    _prepare_retrieval(conn)

    if reset:
        for party in parties_to_run:
            conn.execute("DELETE FROM vote_analyses WHERE party=?", (party,))
        conn.commit()
        log.info(f"Reset analyses for: {parties_to_run}")

    # All items that have at least one vote recorded
    q = ("SELECT DISTINCT i.id, i.title, i.description, i.datum "
         "FROM items i JOIN votes v ON v.item_id = i.id "
         "WHERE i.title IS NOT NULL ORDER BY i.datum DESC")
    if limit:
        q += f" LIMIT {limit}"
    items = conn.execute(q).fetchall()

    # ── Phase 1: build requests ──────────────────────────────────────────────
    log.info("Building batch requests…")
    requests: list[Request] = []
    # section lookup cache: custom_id → list[dict]
    sections_cache: dict[str, list[dict]] = {}
    # vote direction cache: custom_id → str
    vote_dir_cache: dict[str, str] = {}

    skipped_already = skipped_no_vote = 0

    for item in items:
        tags = [r["value"] for r in conn.execute(
            "SELECT value FROM item_tags "
            "WHERE item_id=? AND source IN ('headword','topic')",
            (item["id"],),
        ).fetchall()]

        doc_title, doc_text = get_document_text(conn, item["id"])

        for party in parties_to_run:
            # Skip already-analysed
            if conn.execute(
                "SELECT 1 FROM vote_analyses WHERE item_id=? AND party=?",
                (item["id"], party),
            ).fetchone():
                skipped_already += 1
                continue

            vote_dir = get_party_vote(conn, item["id"], party)
            if not vote_dir:
                skipped_no_vote += 1
                continue

            sections = find_sections(
                conn, party,
                title=item["title"] or "",
                description=item["description"] or "",
                tags=tags,
                item_id=item["id"],
            )

            custom_id = _encode_custom_id(item["id"], party)
            vote_dir_cache[custom_id]  = vote_dir
            sections_cache[custom_id]  = sections

            if not sections and not doc_text:
                # Nothing to work with → score=0 immediately, no API call needed
                store_result(conn, item["id"], party, vote_dir, [],
                             {"score": 0, "relevant_section_index": None,
                              "explanation": "Keine relevanten Wahlprogramm-Passagen oder Dokumenttext gefunden."})
                continue

            prompt = build_prompt(party, vote_dir,
                                  title=item["title"] or "",
                                  description=item["description"] or "",
                                  sections=sections,
                                  doc_title=doc_title,
                                  doc_text=doc_text,
                                  statement=get_party_statement(conn, item["id"], party))
            requests.append(Request(
                custom_id=custom_id,
                params=MessageCreateParamsNonStreaming(
                    model=MODEL,
                    max_tokens=1000,
                    thinking={"type": "disabled"},   # structured task: answer directly
                    messages=[{"role": "user", "content": prompt}],
                ),
            ))

    conn.commit()

    log.info(f"  {skipped_already} already analysed (skipped)")
    log.info(f"  {skipped_no_vote} pairs with no vote data (skipped)")
    log.info(f"  {len(requests)} API requests to submit")

    if not requests:
        log.info("Nothing to do.")
        print_summary(conn)
        conn.close()
        return

    # ── Phase 2: submit batch ────────────────────────────────────────────────
    log.info("Submitting batch…")
    batch = client.messages.batches.create(requests=requests)
    batch_id = batch.id
    log.info(f"Batch submitted: {batch_id}")

    # Persist state so --resume can recover if this process is interrupted
    state = {
        "batch_id":       batch_id,
        "parties":        parties_to_run,
        "limit":          limit,
        "total_requests": len(requests),
        "submitted_at":   time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    BATCH_ID_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    log.info(f"Batch state saved to {BATCH_ID_FILE}")
    log.info("To resume if interrupted: python analyze_votes.py --resume")

    # ── Phase 3: poll ────────────────────────────────────────────────────────
    _poll_and_store(client, conn, batch_id, sections_cache, vote_dir_cache)
    conn.close()


def resume_batch(batch_id: str | None = None) -> None:
    """
    Resume polling a previously submitted batch.
    If batch_id is None, loads it from BATCH_ID_FILE.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    # Resolve batch_id
    if batch_id is None:
        if not BATCH_ID_FILE.exists():
            print(f"No saved batch found at {BATCH_ID_FILE}. "
                  "Pass --resume BATCH_ID explicitly.")
            sys.exit(1)
        state    = json.loads(BATCH_ID_FILE.read_text(encoding="utf-8"))
        batch_id = state["batch_id"]
        log.info(f"Resuming batch {batch_id} "
                 f"(submitted {state.get('submitted_at','?')}, "
                 f"{state.get('total_requests','?')} requests)")

    client = anthropic.Anthropic(api_key=api_key)
    conn   = db.connect()
    db.init_schema(conn)
    _prepare_retrieval(conn)

    # Re-build sections/vote_dir caches from DB for all pending custom_ids
    # We do this by streaming the batch results; for resolved items we need the
    # original sections to map relevant_section_index → section_id.
    # Strategy: look up sections fresh from DB at result-processing time.
    _poll_and_store(client, conn, batch_id,
                    sections_cache=None, vote_dir_cache=None)
    conn.close()


def _poll_and_store(client: anthropic.Anthropic,
                    conn,
                    batch_id: str,
                    sections_cache: dict | None,
                    vote_dir_cache: dict | None) -> None:
    """
    Poll until the batch ends, then iterate results and write to DB.

    sections_cache / vote_dir_cache may be None when resuming a batch from a
    previous process. In that case we re-derive sections from the DB live.
    """
    POLL_INTERVAL = 60  # seconds between status checks

    log.info(f"Polling batch {batch_id} every {POLL_INTERVAL}s…")
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        log.info(f"  status={batch.processing_status}  "
                 f"processing={counts.processing}  "
                 f"succeeded={counts.succeeded}  "
                 f"errored={counts.errored}  "
                 f"expired={counts.expired}")
        if batch.processing_status == "ended":
            break
        time.sleep(POLL_INTERVAL)

    log.info("Batch complete. Processing results…")

    done = contradictions = errors = 0

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        decoded   = _decode_custom_id(custom_id)
        if decoded is None:
            log.warning(f"  Cannot decode custom_id: {custom_id}")
            continue
        item_id, party = decoded

        if result.result.type == "errored":
            log.warning(f"  [{party:5}] item {item_id} errored: "
                        f"{result.result.error}")
            errors += 1
            continue

        if result.result.type == "expired":
            log.warning(f"  [{party:5}] item {item_id} expired (not processed)")
            errors += 1
            continue

        # result.result.type == "succeeded"
        raw = "".join(b.text for b in result.result.message.content if b.type == "text").strip()
        parsed = parse_claude_response(raw)
        if parsed is None:
            log.warning(f"  [{party:5}] item {item_id} – bad JSON: {raw[:80]}")
            errors += 1
            continue

        # Retrieve sections (from cache if available, else re-derive from DB)
        if sections_cache is not None:
            sections = sections_cache.get(custom_id, [])
            vote_dir = vote_dir_cache.get(custom_id, "")  # type: ignore[union-attr]
            # doc text not cached — re-derive from DB
            _, _doc_text = get_document_text(conn, item_id)
        else:
            # Resume path: re-derive from DB
            vote_dir = get_party_vote(conn, item_id, party) or ""
            item_row = conn.execute(
                "SELECT title, description FROM items WHERE id=?", (item_id,)
            ).fetchone()
            tags_rows = conn.execute(
                "SELECT value FROM item_tags "
                "WHERE item_id=? AND source IN ('headword','topic')",
                (item_id,),
            ).fetchall()
            tags = [r["value"] for r in tags_rows]
            sections = find_sections(
                conn, party,
                title=item_row["title"] or "" if item_row else "",
                description=item_row["description"] or "" if item_row else "",
                tags=tags,
                item_id=item_id,
            )
            _, _doc_text = get_document_text(conn, item_id)

        # Skip if already stored (idempotent)
        if conn.execute(
            "SELECT 1 FROM vote_analyses WHERE item_id=? AND party=?",
            (item_id, party),
        ).fetchone():
            continue

        is_contra = store_result(conn, item_id, party, vote_dir, sections, parsed)
        done += 1
        if is_contra:
            contradictions += 1
        score = int(parsed.get("score", 0))
        if score != 0:
            title_row = conn.execute(
                "SELECT title FROM items WHERE id=?", (item_id,)
            ).fetchone()
            title = title_row["title"][:55] if title_row else str(item_id)
            log.info(f"  [{party:5}] {score:+d}  {vote_dir:5}  \"{title}\"")

    log.info(f"Done. {done} analyses written, "
             f"{contradictions} contradictions, {errors} errors.")

    # Clean up saved state file once results are stored
    if BATCH_ID_FILE.exists():
        BATCH_ID_FILE.unlink()
        log.info(f"Removed {BATCH_ID_FILE}")

    verify_contradictions(client, conn)
    print_summary(conn)


# ── Double-check of contradictions ────────────────────────────────────────────
# Single assessments are noisy. Every contradiction (score ≤ -1) is assessed a
# second time with thinking enabled; that more careful answer is stored. If it
# still finds a contradiction, the row is marked checked=1 ("doppelt geprüft"),
# otherwise checked=0 and the contradiction disappears.

def _build_for(conn, item_id: int, party: str) -> tuple[str, list[dict], str]:
    """Prompt, sections and vote direction for one (item, party) – same as the first pass."""
    item = conn.execute("SELECT title, description FROM items WHERE id=?", (item_id,)).fetchone()
    tags = [r["value"] for r in conn.execute(
        "SELECT value FROM item_tags WHERE item_id=? AND source IN ('headword','topic')", (item_id,))]
    vote_dir = get_party_vote(conn, item_id, party) or ""
    sections = find_sections(conn, party, item["title"] or "", item["description"] or "", tags,
                             item_id=item_id)
    doc_title, doc_text = get_document_text(conn, item_id)
    prompt = build_prompt(party, vote_dir, item["title"] or "", item["description"] or "", sections,
                          doc_title=doc_title, doc_text=doc_text,
                          statement=get_party_statement(conn, item_id, party))
    return prompt, sections, vote_dir


def verify_contradictions(client: anthropic.Anthropic, conn) -> None:
    rows = conn.execute(
        "SELECT item_id, party FROM vote_analyses WHERE score <= -1 AND checked IS NULL"
    ).fetchall()
    if not rows:
        return
    log.info(f"Double-checking {len(rows)} contradictions with thinking enabled …")
    built, reqs = {}, []
    for r in rows:
        prompt, sections, vote_dir = _build_for(conn, r["item_id"], r["party"])
        cid = "chk_" + _encode_custom_id(r["item_id"], r["party"])[len("vote_"):]
        built[cid] = (r["item_id"], r["party"], sections, vote_dir)
        reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=MODEL, max_tokens=6000,        # room for thinking + answer
            messages=[{"role": "user", "content": prompt}],
        )))
    batch = client.messages.batches.create(requests=reqs)
    log.info(f"Verification batch submitted: {batch.id}")
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        time.sleep(60)

    confirmed = rejected = failed = 0
    for res in client.messages.batches.results(batch.id):
        item_id, party, sections, vote_dir = built[res.custom_id]
        parsed = None
        if res.result.type == "succeeded":
            text = "".join(b.text for b in res.result.message.content if b.type == "text")
            parsed = parse_claude_response(text)
        if parsed is None:
            failed += 1          # keep the first answer, unchecked
            continue
        store_result(conn, item_id, party, vote_dir, sections, parsed)
        ok = int(parsed["score"]) <= -1
        conn.execute("UPDATE vote_analyses SET checked=? WHERE item_id=? AND party=?",
                     (1 if ok else 0, item_id, party))
        conn.commit()
        confirmed += ok
        rejected += not ok
    log.info(f"Verification: {confirmed} contradictions confirmed, {rejected} withdrawn, "
             f"{failed} could not be checked.")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--party",  nargs="+", default=PARTIES,
                   help="Parties to analyse (default: all 5)")
    p.add_argument("--limit",  type=int, default=None,
                   help="Max number of voted items to process")
    p.add_argument("--reset",  action="store_true",
                   help="Delete existing analyses for selected parties")
    p.add_argument("--batch",  action="store_true",
                   help="Use Anthropic Message Batches API (50%% discount, ~1h)")
    p.add_argument("--resume", nargs="?", const=True, metavar="BATCH_ID",
                   help="Poll + store results of a previous batch "
                        "(pass BATCH_ID or omit to load from saved file)")
    p.add_argument("--retrieval", choices=["embedding", "keyword"], default=RETRIEVAL,
                   help="How manifesto passages are found (default: embedding)")
    args = p.parse_args()
    RETRIEVAL = args.retrieval

    if args.resume:
        # --resume [BATCH_ID]
        batch_id_arg = args.resume if isinstance(args.resume, str) else None
        resume_batch(batch_id_arg)
    elif args.batch:
        run_batch(args.party, limit=args.limit, reset=args.reset)
    else:
        run(args.party, limit=args.limit, reset=args.reset)
