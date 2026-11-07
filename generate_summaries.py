"""
Generates HTML Kurzinformation for items that have none, using Claude.
Reads document text from the DB, writes back to items.shortinfo.

Usage:
    python generate_summaries.py
"""
import os
import re
import time
import logging
import anthropic
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SYSTEM = """Du bist ein neutraler parlamentarischer Assistent.
Fasse den österreichischen Gesetzestext in 2–4 prägnanten Bullet Points auf Deutsch zusammen.
Antworte NUR mit einem HTML-Fragment: eine <ul> mit <li>-Einträgen, kein weiterer Text."""

MODEL = "claude-haiku-5-5"


def build_prompt(title: str, description: str | None, doc_text: str) -> str:
    parts = [f"Titel: {title}"]
    if description:
        parts.append(f"Vollbezeichnung: {description}")
    excerpt = doc_text[:3000] if doc_text else ""
    if excerpt:
        parts.append(f"Gesetzestext (Auszug):\n{excerpt}")
    return "\n\n".join(parts)


def generate(client: anthropic.Anthropic, title: str, description: str | None, doc_text: str) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        thinking={"type": "disabled"},   # structured task: answer directly
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(title, description, doc_text)}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    # Strip markdown code fences if model included them
    text = re.sub(r'^```[a-z]*\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY nicht gesetzt.")

    client = anthropic.Anthropic(api_key=api_key)
    conn = db.connect()

    pending = conn.execute("""
        SELECT i.id, i.title, i.description,
               (SELECT d.text FROM documents d
                WHERE d.item_id = i.id
                ORDER BY CASE d.title
                    WHEN 'Gesetzestext' THEN 1
                    WHEN 'Initiativantrag (elektr. übermittelte Version)' THEN 1
                    ELSE 2 END
                LIMIT 1) AS doc_text
        FROM items i
        WHERE i.shortinfo IS NULL OR i.shortinfo = ''
        ORDER BY i.id
    """).fetchall()

    log.info(f"{len(pending)} Items ohne Kurzinformation")

    for i, row in enumerate(pending):
        doc_text = row["doc_text"] or ""
        try:
            html = generate(client, row["title"], row["description"], doc_text)
            conn.execute("UPDATE items SET shortinfo=?, shortinfo_llm=1 WHERE id=?", (html, row["id"]))
            conn.commit()
            log.info(f"[{i+1}/{len(pending)}] {row['title'][:60]}")
        except Exception as e:
            log.warning(f"Fehler bei id={row['id']}: {e}")
        time.sleep(0.3)

    log.info("Fertig.")
    conn.close()


if __name__ == "__main__":
    main()
