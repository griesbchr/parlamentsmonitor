"""
Austrian Parliament Monitor – Flask Dashboard
"""
import html as htmllib
import json
import re
import time
from datetime import date
from flask import Flask, jsonify, render_template, Response
import requests
import database as db

app = Flask(__name__)

# ── Tagungen cache ────────────────────────────────────────────────────────────
_tagungen_cache: dict = {"data": None, "ts": 0.0}
TAGUNGEN_TTL = 3600 * 12   # refresh every 12 hours
TAGUNGEN_API = "https://www.parlament.gv.at/Filter/api/filter/data/701"
MONTH_DE = ["", "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
            "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


def _tagung_label(nr: int, von: str, bis: str, ongoing: bool) -> str:
    try:
        v = date.fromisoformat(von)
        if ongoing:
            return f"{nr}. Tagung (ab {MONTH_DE[v.month]} {v.year})"
        b = date.fromisoformat(bis)
        return (f"{nr}. Tagung "
                f"({MONTH_DE[v.month]} {v.year} – {MONTH_DE[b.month]} {b.year})")
    except Exception:
        return f"{nr}. Tagung"


def fetch_tagungen() -> list:
    """Fetch all Tagungen for XXVIII. GP from the Parliament sessions API."""
    body = {"GREMIUM": ["NR"], "GP_CODE": ["XXVIII"],
            "SIAKT": ["SI"], "INDH": ["J"]}
    hdrs = {"User-Agent": "AustrianParliamentMonitor/1.0"}
    try:
        resp = requests.post(TAGUNGEN_API, json=body, headers=hdrs, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        all_rows = list(data.get("rows", []))
        for page in range(2, data.get("pages", 1) + 1):
            r = requests.post(TAGUNGEN_API, params={"page": page},
                              json=body, headers=hdrs, timeout=10)
            r.raise_for_status()
            all_rows.extend(r.json().get("rows", []))

        # col 6 = Tagung key (DDMMYYYY), col 7 = description, col 12 = session date
        tagmap: dict = {}
        for row in all_rows:
            key, desc, sdt = row[6], row[7], row[12]
            if key not in tagmap:
                tagmap[key] = {"desc": desc or "", "dates": []}
            if sdt:
                tagmap[key]["dates"].append(sdt[:10])

        def key_to_iso(k: str) -> str:
            # DDMMYYYY → YYYY-MM-DD for chronological sorting
            return f"{k[4:8]}-{k[2:4]}-{k[0:2]}"

        result = []
        for nr, key in enumerate(sorted(tagmap.keys(), key=key_to_iso), 1):
            t = tagmap[key]
            dates = sorted(t["dates"])
            von = dates[0] if dates else None
            bis = dates[-1] if dates else None
            ongoing = "- " not in t["desc"]   # "ab …" = ongoing; "DD.MM – DD.MM" = closed
            result.append({
                "nr":      nr,
                "key":     key,
                "label":   _tagung_label(nr, von, bis, ongoing) if von else f"{nr}. Tagung",
                "von":     von,    # YYYY-MM-DD
                "bis":     bis,    # YYYY-MM-DD (last known session, or end of Tagung)
                "ongoing": ongoing,
            })
        return result
    except Exception as exc:
        app.logger.warning("fetch_tagungen failed: %s", exc)
        return []


CLUB_COLORS = {
    "ÖVP":   "#62C3D0",
    "SPÖ":   "#FF0000",
    "FPÖ":   "#0052FB",
    "GRÜNE": "#69B12E",
    "NEOS":  "#E3257B",
}


def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text).strip()


def teaser(shortinfo: str | None, max_len: int = 180) -> str:
    """First bullet point of the summary as plain text, for the list cards."""
    if not shortinfo:
        return ""
    m = re.search(r"<li[^>]*>(.*?)</li>", shortinfo, re.S | re.I)
    text = htmllib.unescape(strip_html(m.group(1) if m else shortinfo))
    text = re.sub(r"\s+", " ", re.sub(r"^Kurzinformation\s*", "", text)).strip()
    return text if len(text) <= max_len else text[:max_len].rsplit(" ", 1)[0] + " …"


def get_conn():
    return db.connect()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tagungen")
def api_tagungen():
    global _tagungen_cache
    now = time.time()
    if _tagungen_cache["data"] is None or now - _tagungen_cache["ts"] > TAGUNGEN_TTL:
        _tagungen_cache["data"] = fetch_tagungen()
        _tagungen_cache["ts"] = now
    return jsonify(_tagungen_cache["data"])


@app.route("/api/items")
def api_items():
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            i.id, i.zitation, i.title, i.datum, i.doktyp, i.doktyp_lang,
            i.description, i.status_text,
            GROUP_CONCAT(
                v.club_name || ':' || v.in_favor || ':' || COALESCE(v.fraction_count,''),
                '|'
            ) AS votes_raw,
            i.media_score, i.shortinfo,
            (SELECT MIN(score) FROM vote_analyses WHERE item_id = i.id) AS min_score,
            (SELECT COUNT(*)   FROM vote_analyses WHERE item_id = i.id) AS analysis_count
        FROM items i
        LEFT JOIN votes v ON v.item_id = i.id
        GROUP BY i.id
        ORDER BY i.datum DESC, i.zitation
    """).fetchall()
    scores: dict[int, dict[str, int]] = {}
    for a in conn.execute("SELECT item_id, party, score FROM vote_analyses"):
        scores.setdefault(a["item_id"], {})[a["party"]] = a["score"]
    tags: dict[int, dict[str, list[str]]] = {}
    for t in conn.execute("SELECT item_id, source, value FROM item_tags "
                          "WHERE source IN ('topic', 'headword') ORDER BY value"):
        tags.setdefault(t["item_id"], {}).setdefault(t["source"], []).append(t["value"])
    conn.close()

    items = []
    for r in rows:
        votes = []
        if r["votes_raw"]:
            for part in r["votes_raw"].split("|"):
                name, favor, seats = part.split(":")
                votes.append({
                    "club": name,
                    "in_favor": favor == "1",
                    "seats": int(seats) if seats else None,
                    "color": CLUB_COLORS.get(name, "#999"),
                })
        items.append({
            "id":             r["id"],
            "zitation":       r["zitation"],
            "title":          r["title"],
            "datum":          r["datum"],
            "doktyp":         r["doktyp"],
            "doktyp_lang":    r["doktyp_lang"],
            "description":    r["description"],
            "status_text":    strip_html(r["status_text"] or ""),
            "votes":          votes,
            "media_score":    r["media_score"] or 0,
            "min_score":      r["min_score"],        # lowest party score (None if no analysis)
            "analysis_count": r["analysis_count"] or 0,
            "scores":         scores.get(r["id"], {}),   # {party: -2..+2}
            "teaser":         teaser(r["shortinfo"]),
            "topics":         tags.get(r["id"], {}).get("topic", []),
            "headwords":      tags.get(r["id"], {}).get("headword", []),
        })

    return jsonify(items)


# ── Topics ────────────────────────────────────────────────────────────────────
# The parliament's 18 official topics, mapped to the policy areas that
# index_manifestos.py assigns to manifesto sections.
TOPIC_AREAS: dict[str, list[str]] = {
    "Inneres und Recht":                     ["Sicherheit", "Migration", "Demokratie"],
    "Budget und Finanzen":                   ["Wirtschaft"],
    "Wirtschaft":                            ["Wirtschaft"],
    "Soziales":                              ["Soziales"],
    "Arbeit":                                ["Arbeit"],
    "Gesundheit und Ernährung":              ["Gesundheit"],
    "Klima, Umwelt und Energie":             ["Klimaschutz"],
    "Verkehr und Infrastruktur":             ["Verkehr"],
    "Information und Medien":                ["Demokratie", "Digitalisierung"],
    "Parlament und Demokratie":              ["Demokratie"],
    "Familie und Generationen":              ["Familie"],
    "Europäische Union":                     ["Außenpolitik"],
    "Bildung":                               ["Bildung"],
    "Außenpolitik":                          ["Außenpolitik"],
    "Land- und Forstwirtschaft":             ["Landwirtschaft"],
    "Kultur":                                [],
    "Innovation, Technologie und Forschung": ["Digitalisierung"],
    "Frauen und Gleichbehandlung":           ["Familie"],
}


def topic_slug(name: str) -> str:
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


@app.route("/api/themen")
def api_topics():
    conn = get_conn()
    names = [r["value"] for r in conn.execute(
        "SELECT DISTINCT value FROM item_tags WHERE source='topic' ORDER BY value")]
    conn.close()
    return jsonify([{"name": n, "slug": topic_slug(n), "areas": TOPIC_AREAS.get(n, [])}
                    for n in names])


@app.route("/api/themen/<slug>")
def api_topic(slug):
    """Manifesto passages for a topic, per party, in manifesto order."""
    conn = get_conn()
    names = [r["value"] for r in conn.execute(
        "SELECT DISTINCT value FROM item_tags WHERE source='topic'")]
    name = next((n for n in names if topic_slug(n) == slug), None)
    if name is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    areas = TOPIC_AREAS.get(name, [])
    sections: dict[str, list[dict]] = {}
    if areas:
        rows = conn.execute(
            f"SELECT party, section_path, text FROM manifesto_sections "
            f"WHERE policy_area IN ({','.join('?' * len(areas))}) ORDER BY id",
            areas,
        ).fetchall()
        for r in rows:
            sections.setdefault(r["party"], []).append(
                {"path": r["section_path"], "text": r["text"]})
    conn.close()
    return jsonify({"name": name, "slug": slug, "areas": areas, "sections": sections})


@app.route("/api/items/<int:item_id>")
def api_item_detail(item_id):
    conn = get_conn()

    item = conn.execute("""
        SELECT id, zitation, title, description, shortinfo, shortinfo_llm,
               status_text, datum, doktyp, doktyp_lang, his_url
        FROM items WHERE id = ?
    """, (item_id,)).fetchone()

    if not item:
        return jsonify({"error": "not found"}), 404

    votes = conn.execute("""
        SELECT club_name, in_favor, fraction_count
        FROM votes WHERE item_id = ? ORDER BY club_name
    """, (item_id,)).fetchall()

    tags = conn.execute("""
        SELECT source, GROUP_CONCAT(value, ', ') AS vals
        FROM item_tags WHERE item_id = ?
        GROUP BY source
    """, (item_id,)).fetchall()

    docs = conn.execute("""
        SELECT id, title, url, html FROM documents
        WHERE item_id = ?
        ORDER BY CASE title
            WHEN 'Gesetzestext' THEN 1
            WHEN 'Initiativantrag (elektr. übermittelte Version)' THEN 1
            WHEN 'Erläuterungen' THEN 2
            WHEN 'Textgegenüberstellung' THEN 3
            ELSE 4 END, title
    """, (item_id,)).fetchall()

    conn.close()

    return jsonify({
        "id":          item["id"],
        "zitation":    item["zitation"],
        "title":       item["title"],
        "description": item["description"],
        "shortinfo":     item["shortinfo"],
        "shortinfo_llm": bool(item["shortinfo_llm"]),
        "status_text": strip_html(item["status_text"] or ""),
        "datum":       item["datum"],
        "doktyp":      item["doktyp"],
        "doktyp_lang": item["doktyp_lang"],
        "his_url":     item["his_url"],
        "votes": [
            {
                "club":     v["club_name"],
                "in_favor": bool(v["in_favor"]),
                "seats":    v["fraction_count"],
                "color":    CLUB_COLORS.get(v["club_name"], "#999"),
            }
            for v in votes
        ],
        "tags":  {r["source"]: r["vals"] for r in tags},
        "documents": [
            {"id": d["id"], "title": d["title"], "url": d["url"], "has_html": bool(d["html"])}
            for d in docs
        ],
    })


@app.route("/api/items/<int:item_id>/analyses")
def api_item_analyses(item_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT va.party, va.party_vote, va.score, va.explanation,
               ms.text        AS section_text,
               ms.section_path,
               ps.statement, ps.speakers, ps.sources
        FROM vote_analyses va
        LEFT JOIN manifesto_sections ms ON ms.id = va.section_id
        LEFT JOIN party_statements  ps ON ps.item_id = va.item_id AND ps.party = va.party
        WHERE va.item_id = ?
        ORDER BY va.score ASC
    """, (item_id,)).fetchall()
    conn.close()
    return jsonify([{
        "party":        r["party"],
        "party_vote":   r["party_vote"],
        "score":        r["score"],
        "explanation":  r["explanation"],
        "section_text": r["section_text"],
        "section_path": r["section_path"],
        "statement":    r["statement"],
        "speakers":     json.loads(r["speakers"]) if r["speakers"] else [],
        "sources":      json.loads(r["sources"]) if r["sources"] else [],
    } for r in rows])


@app.route("/doc/<int:doc_id>")
def serve_document(doc_id):
    conn = get_conn()
    row = conn.execute("SELECT html FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    if not row or not row["html"]:
        return "Kein HTML verfügbar.", 404
    return Response(row["html"], mimetype="text/html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
