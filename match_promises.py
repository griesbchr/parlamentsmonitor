"""
match_promises.py – Match extracted manifesto promises against parliament votes.

Strategy
--------
For each DB item that was voted on:
1. Build a short description (title + description + policy tags).
2. For each party that had a recorded vote on that item, check whether
   any of that party's manifesto promises are relevant.
3. Claude evaluates relevance and alignment (supports / contradicts / neutral).
4. Store results in promise_matches table.

This is done incrementally: already-matched (promise_id, item_id) pairs are skipped.

Usage
-----
    python match_promises.py                      # all items
    python match_promises.py --party ÖVP          # only ÖVP promises
    python match_promises.py --min-specificity 4  # only specific promises (4-5)
    python match_promises.py --limit 50           # process only 50 items
"""

import json
import re
import sys
import os
import logging
from pathlib import Path

import anthropic
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"

MATCH_PROMPT = """\
Du analysierst österreichische Parlamentsbeschlüsse im Vergleich zu Wahlversprechen.

**Parlamentsbeschluss:**
Titel: {title}
Datum: {datum}
Typ: {doktyp_lang}
Beschreibung: {description}
Themen: {tags}

**Wahlversprechen der Partei {party}:**
{promises_block}

Für jedes Versprechen, das thematisch relevant zu diesem Beschluss ist:
1. Beurteile die Relevanz (0.0–1.0)
2. Beurteile die Ausrichtung:
   - "supports" = Beschluss setzt Versprechen um oder fördert das Ziel
   - "contradicts" = Beschluss widerspricht dem Versprechen
   - "neutral" = thematisch verwandt, aber keine klare Aussage
3. Kurze Begründung (1 Satz auf Deutsch)

Antworte NUR mit validem JSON:
[
  {{
    "promise_id": <integer>,
    "relevance_score": 0.0,
    "alignment": "supports|contradicts|neutral",
    "explanation": "..."
  }},
  ...
]

Nur Versprechen mit Relevanz >= 0.3 aufnehmen.
Falls keine Versprechen relevant sind, antworte mit: []
"""


def item_description(item) -> dict:
    return {
        "title":       item["title"] or "",
        "datum":       item["datum"] or "",
        "doktyp_lang": item["doktyp_lang"] or "",
        "description": (item["description"] or "")[:500],
        "tags":        item["tags"] or "",
    }


def format_promises_block(promises: list) -> str:
    lines = []
    for p in promises:
        lines.append(f"[ID {p['id']}] ({p['policy_area']}) {p['promise_text']}")
    return "\n".join(lines)


def match_item_to_promises(
    client: anthropic.Anthropic,
    item_info: dict,
    party: str,
    promises: list,
) -> list[dict]:
    if not promises:
        return []

    # Batch promises in groups of 30 to avoid prompt overflow
    results = []
    for i in range(0, len(promises), 30):
        batch = promises[i : i + 30]
        prompt = MATCH_PROMPT.format(
            title=item_info["title"],
            datum=item_info["datum"],
            doktyp_lang=item_info["doktyp_lang"],
            description=item_info["description"],
            tags=item_info["tags"],
            party=party,
            promises_block=format_promises_block(batch),
        )
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(b.text for b in msg.content if b.type == "text").strip()
            json_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                results.extend(parsed if isinstance(parsed, list) else [])
        except Exception as e:
            log.warning(f"  Match call failed: {e}")

    return results


def run(party_filter: str | None, min_specificity: int, limit: int | None):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY nicht gesetzt.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    conn   = db.connect()

    # Load promises grouped by party
    spec_filter = f"AND specificity >= {min_specificity}" if min_specificity > 1 else ""
    party_sql   = f"AND party = '{party_filter}'" if party_filter else ""

    promises_by_party: dict[str, list] = {}
    rows = conn.execute(
        f"SELECT id, party, promise_text, policy_area FROM manifesto_promises "
        f"WHERE 1=1 {spec_filter} {party_sql}"
    ).fetchall()
    for r in rows:
        promises_by_party.setdefault(r["party"], []).append(dict(r))

    if not promises_by_party:
        log.error("No promises found – run extract_promises.py first")
        sys.exit(1)

    total_promises = sum(len(v) for v in promises_by_party.values())
    log.info(f"Loaded {total_promises} promises for {list(promises_by_party.keys())}")

    # Load items with votes
    items_sql = f"SELECT LIMIT {limit}" if limit else "SELECT"
    items = conn.execute("""
        SELECT i.id, i.title, i.datum, i.doktyp_lang, i.description,
               GROUP_CONCAT(DISTINCT it.value) AS tags,
               GROUP_CONCAT(DISTINCT v.club_name) AS voting_parties
        FROM items i
        LEFT JOIN item_tags it ON it.item_id = i.id AND it.source = 'topic'
        LEFT JOIN votes v ON v.item_id = i.id
        GROUP BY i.id
        HAVING voting_parties IS NOT NULL
        ORDER BY i.datum DESC
    """).fetchall()

    if limit:
        items = items[:limit]

    log.info(f"Processing {len(items)} items with votes …")

    total_matches = 0
    for idx, item in enumerate(items):
        voting_parties = set((item["voting_parties"] or "").split(","))
        item_info = {
            "title":       item["title"] or "",
            "datum":       item["datum"] or "",
            "doktyp_lang": item["doktyp_lang"] or "",
            "description": (item["description"] or "")[:500],
            "tags":        (item["tags"] or "")[:200],
        }

        for party, promises in promises_by_party.items():
            if party not in voting_parties:
                continue  # party didn't vote on this item

            # Check how many promise-item pairs already matched
            already = conn.execute(
                """SELECT COUNT(*) FROM promise_matches pm
                   JOIN manifesto_promises mp ON mp.id = pm.promise_id
                   WHERE pm.item_id=? AND mp.party=?""",
                (item["id"], party),
            ).fetchone()[0]

            if already > 0:
                continue  # already processed this (item, party) pair

            matches = match_item_to_promises(client, item_info, party, promises)

            if matches:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO promise_matches
                        (promise_id, item_id, alignment, relevance_score, explanation)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            m["promise_id"],
                            item["id"],
                            m.get("alignment", "neutral"),
                            float(m.get("relevance_score", 0)),
                            m.get("explanation", ""),
                        )
                        for m in matches
                        if isinstance(m.get("promise_id"), int)
                    ],
                )
                conn.commit()
                total_matches += len(matches)

        if (idx + 1) % 20 == 0:
            log.info(f"  {idx+1}/{len(items)} items processed, {total_matches} matches so far")

    log.info(f"Done. {total_matches} promise-vote matches stored.")

    # Summary
    print("\n=== Match-Zusammenfassung ===")
    rows = conn.execute("""
        SELECT mp.party, pm.alignment, COUNT(*) as n
        FROM promise_matches pm
        JOIN manifesto_promises mp ON mp.id = pm.promise_id
        GROUP BY mp.party, pm.alignment
        ORDER BY mp.party, pm.alignment
    """).fetchall()
    current_party = None
    for r in rows:
        if r["party"] != current_party:
            current_party = r["party"]
            print(f"\n  {current_party}:")
        print(f"    {r['alignment']:<15} {r['n']:>4} Treffer")

    conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--party",           default=None, help="Filter by party")
    p.add_argument("--min-specificity", type=int, default=3)
    p.add_argument("--limit",           type=int, default=None)
    args = p.parse_args()
    run(args.party, args.min_specificity, args.limit)
