"""
fetch_statements.py – Why did each party vote the way it did?

Step 1 – fetch  (no API cost)
  For every voted item, follow the Parlamentskorrespondenz (PK) links that the
  parliament API lists under content.correspondence, download each article and
  keep only the section about this item (PK articles often cover several items;
  each item's section starts at an <a id="XXVIII_I_00069"> style anchor).
  Stored in pk_articles.

Step 2 – extract  (Claude, Batch API)
  One request per item: vote result + PK sections -> for each party the reasons
  it gave, in 2-3 German sentences, plus speaker names and which articles were
  used. Only what the articles say; nothing found -> statement NULL.
  Stored in party_statements.

Usage
-----
    python fetch_statements.py              # fetch + extract for items not done yet
    python fetch_statements.py --fetch      # only download articles
    python fetch_statements.py --extract    # only run Claude on fetched articles
    python fetch_statements.py --reset      # redo the Claude extraction for all items
"""

import html as htmllib
import json
import logging
import os
import re
import sys
import time

import anthropic
import requests
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

MODEL       = "claude-haiku-5-5"
BASE_URL    = "https://www.parlament.gv.at"
HEADERS     = {"User-Agent": "AustrianParliamentMonitor/1.0"}
RATE_LIMIT  = 0.3      # seconds between page downloads
MAX_SECTION = 12000    # chars kept per article section
MAX_PROMPT_ARTICLES_CHARS = 40000

PARTIES = ["ÖVP", "SPÖ", "FPÖ", "GRÜNE", "NEOS"]
CLUB_TO_PARTY = {"V": "ÖVP", "S": "SPÖ", "F": "FPÖ", "G": "GRÜNE", "N": "NEOS"}

_ANCHOR_RE = re.compile(r'<a id="(XXVIII_[A-Z]+_\d+)"\s*>\s*</a>')


# ── Step 1: fetch ─────────────────────────────────────────────────────────────

def html_to_text(fragment: str) -> str:
    fragment = re.sub(r'<(script|style)\b.*?</\1>', ' ', fragment, flags=re.S | re.I)
    fragment = re.sub(r'</(p|h\d|li|div)>|<br\s*/?>', '\n', fragment, flags=re.I)
    text = htmllib.unescape(re.sub(r'<[^>]+>', ' ', fragment))
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    return re.sub(r'\s*\n\s*', '\n', text).strip()


def article_body(page: str) -> str:
    """The PK article sits in <div class=pk >; cut before the related-links bubbles."""
    start = page.find('<div class=pk')
    if start < 0:
        return ""
    end = page.find('bubbles-container', start)
    if end > 0:
        end = page.rfind('<', start, end)   # back up to the start of that tag
    return page[start:end if end > 0 else start + 200000]


def item_section(body: str, anchor: str) -> str:
    """
    Text from the item's anchor to the next anchor group. Anchors that sit
    directly next to each other mark one section covering several items.
    Falls back to the whole article if the anchor isn't there.
    """
    anchors = list(_ANCHOR_RE.finditer(body))
    idx = next((i for i, m in enumerate(anchors) if m.group(1) == anchor), None)
    if idx is None:
        return html_to_text(body)
    start = anchors[idx].start()
    # skip over the rest of this anchor group (adjacent anchors, nothing between them)
    j = idx
    while j + 1 < len(anchors) and not body[anchors[j].end():anchors[j + 1].start()].strip():
        j += 1
    end = anchors[j + 1].start() if j + 1 < len(anchors) else len(body)
    return html_to_text(body[start:end])


def correspondence_links(raw_json: str) -> list[dict]:
    try:
        d = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return []
    content = d.get("content", d)
    return [c for c in (content.get("correspondence") or []) if c.get("url")]


def fetch(conn) -> None:
    items = conn.execute("""
        SELECT i.id, i.raw_json FROM items i
        WHERE i.raw_json IS NOT NULL
          AND i.id IN (SELECT item_id FROM votes)
    """).fetchall()

    todo = []   # (item_id, link)
    for it in items:
        for link in correspondence_links(it["raw_json"]):
            exists = conn.execute("SELECT 1 FROM pk_articles WHERE item_id=? AND url=?",
                                  (it["id"], link["url"])).fetchone()
            if not exists:
                todo.append((it["id"], link))

    pages = {}  # page url (without anchor) -> article body html
    log.info(f"{len(todo)} article sections to fetch "
             f"({len({l['url'].split('#')[0] for _, l in todo})} distinct pages)")

    for n, (item_id, link) in enumerate(todo, 1):
        page_url, _, anchor = link["url"].partition("#")
        if page_url not in pages:
            try:
                r = requests.get(BASE_URL + page_url, headers=HEADERS, timeout=20)
                r.raise_for_status()
                pages[page_url] = article_body(r.text)
            except requests.RequestException as e:
                log.warning(f"  {page_url}: {e}")
                pages[page_url] = None
            time.sleep(RATE_LIMIT)
        body = pages[page_url]
        if body is None:
            continue   # retried on the next run
        text = item_section(body, anchor)[:MAX_SECTION] if body else ""
        conn.execute(
            "INSERT OR REPLACE INTO pk_articles (item_id, url, title, date, text) "
            "VALUES (?,?,?,?,?)",
            (item_id, link["url"], link.get("title"), link.get("date"), text),
        )
        if n % 50 == 0:
            conn.commit()
            log.info(f"  {n}/{len(todo)}")
    conn.commit()
    log.info("Fetch done.")


# ── Step 2: extract ───────────────────────────────────────────────────────────

_PROMPT = """\
Unten stehen Berichte der Parlamentskorrespondenz (PK) des österreichischen Parlaments \
zu einem Beschluss des Nationalrats. Fasse für jede Partei zusammen, wie sie ihr \
Abstimmungsverhalten begründet hat.

## Beschluss
Titel: {title}
Abstimmung im Nationalrat: {votes}

## PK-Berichte
{articles}

## Aufgabe
Für jede der Parteien ÖVP, SPÖ, FPÖ, GRÜNE, NEOS:
- "statement": höchstens 3 kurze Sätze auf Deutsch: Welche Gründe und Argumente hat die Partei laut \
den Berichten für ihre Position genannt? Nenne konkrete Argumente, nicht nur "stimmte zu". \
Verwende NUR, was in den Berichten steht. Wenn die Berichte nichts zur Position dieser \
Partei zu diesem Beschluss enthalten: null.
- "speakers": Namen der Abgeordneten/Regierungsmitglieder dieser Partei, deren Aussagen \
du verwendet hast (Liste, ggf. leer).
- "sources": Nummern der Berichte [0], [1], … die du für diese Partei verwendet hast.

Aussagen im Bundesrat zählen, wenn sie die Position der Partei zu diesem Beschluss zeigen. \
Grüne heißen in den Berichten oft "Grüne" oder "die Grünen".

Antworte NUR mit validem JSON:
{{"ÖVP": {{"statement": "...", "speakers": [], "sources": []}}, "SPÖ": {{...}}, \
"FPÖ": {{...}}, "GRÜNE": {{...}}, "NEOS": {{...}}}}"""


def vote_summary(conn, item_id: int) -> str:
    rows = conn.execute("SELECT club_code, in_favor FROM votes WHERE item_id=?",
                        (item_id,)).fetchall()
    parts = [f"{CLUB_TO_PARTY.get(r['club_code'], r['club_code'])}: "
             f"{'dafür' if r['in_favor'] == 1 else 'dagegen'}" for r in rows]
    return ", ".join(parts) or "unbekannt"


def parse_json(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def build_requests(conn, reset: bool) -> tuple[list[Request], dict]:
    if reset:
        conn.execute("DELETE FROM party_statements")
        conn.commit()

    items = conn.execute("""
        SELECT DISTINCT i.id, i.title FROM items i
        JOIN pk_articles p ON p.item_id = i.id
        WHERE i.id NOT IN (SELECT item_id FROM party_statements)
          AND LENGTH(p.text) > 100
    """).fetchall()

    reqs, article_map = [], {}
    for it in items:
        arts = conn.execute(
            "SELECT title, url, date, text FROM pk_articles "
            "WHERE item_id=? AND LENGTH(text) > 100",
            (it["id"],),
        ).fetchall()
        # sort chronologically (committee report, then plenary, then Bundesrat)
        arts = sorted(arts, key=lambda a: (a["date"] or "")[6:] + (a["date"] or "")[3:5]
                      + (a["date"] or "")[:2])
        budget = MAX_PROMPT_ARTICLES_CHARS // max(1, len(arts))
        blocks = [f"[{i}] {a['title']} ({a['date']})\n{a['text'][:budget]}"
                  for i, a in enumerate(arts)]
        article_map[it["id"]] = [{"title": a["title"], "url": a["url"], "date": a["date"]}
                                 for a in arts]
        prompt = _PROMPT.format(title=it["title"], votes=vote_summary(conn, it["id"]),
                                articles="\n\n".join(blocks))
        reqs.append(Request(
            custom_id=f"stmt_{it['id']}",
            params=MessageCreateParamsNonStreaming(
                model=MODEL, max_tokens=4000, thinking={"type": "disabled"},
                messages=[{"role": "user", "content": prompt}],
            ),
        ))
    return reqs, article_map


def extract(conn, reset: bool = False) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    reqs, article_map = build_requests(conn, reset)
    log.info(f"{len(reqs)} items to extract statements for")
    if not reqs:
        return

    batch = client.messages.batches.create(requests=reqs)
    log.info(f"Batch submitted: {batch.id}")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        c = b.request_counts
        log.info(f"  status={b.processing_status} processing={c.processing} "
                 f"succeeded={c.succeeded} errored={c.errored}")
        if b.processing_status == "ended":
            break
        time.sleep(60)

    found = errors = 0
    for res in client.messages.batches.results(batch.id):
        item_id = int(res.custom_id.split("_")[1])
        if res.result.type != "succeeded":
            errors += 1
            continue
        # the model may return a thinking block before the answer – use only text blocks
        text = "".join(b.text for b in res.result.message.content if b.type == "text")
        if res.result.message.stop_reason == "max_tokens":
            log.warning(f"  item {item_id}: answer cut off (max_tokens)")
        data = parse_json(text)
        if data is None:
            log.warning(f"  item {item_id}: bad JSON")
            errors += 1
            continue
        arts = article_map.get(item_id, [])
        for party in PARTIES:
            entry = data.get(party) or {}
            stmt = entry.get("statement") or None
            srcs = [arts[i] for i in entry.get("sources") or []
                    if isinstance(i, int) and 0 <= i < len(arts)]
            conn.execute(
                "INSERT OR REPLACE INTO party_statements "
                "(item_id, party, statement, speakers, sources) VALUES (?,?,?,?,?)",
                (item_id, party, stmt,
                 json.dumps(entry.get("speakers") or [], ensure_ascii=False),
                 json.dumps(srcs, ensure_ascii=False)),
            )
            found += stmt is not None
        conn.commit()
    log.info(f"Extract done. {found} party statements found, {errors} errors.")


if __name__ == "__main__":
    args = set(sys.argv[1:])
    conn = db.connect()
    db.init_schema(conn)
    only_fetch, only_extract = "--fetch" in args, "--extract" in args
    if not only_extract:
        fetch(conn)
    if not only_fetch:
        extract(conn, reset="--reset" in args)
    conn.close()
