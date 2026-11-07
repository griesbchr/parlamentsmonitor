"""
extract_manifestos.py – Layout-aware text extraction from the manifesto PDFs.

Replaces the old pypdf text + clean_manifestos.py heuristics. PyMuPDF gives each
line's position and font size, which makes the structure reliable:

  * body text   = the most common font size in the document
  * headings    = clearly larger lines (H1 ≥ 1.8× body, H2 ≥ 1.12× body);
                  consecutive heading lines are merged ("Umweltschutz-" + "und Klimaschutz")
  * columns     = detected per page from where body lines start; read left to right
  * dropped     = page headers/footers (top/bottom margin), decorative giant
                  lettering (> 40 pt) and text outside the page
  * paragraphs  = rebuilt from vertical gaps, short last lines and bullets;
                  hyphenated line breaks are repaired ("Technologie-" + "offenheit")

Output: manifestos/<party>_clean.txt
    # Heading 1
    ## Heading 2
    paragraph text on one line

    next paragraph …

Usage
-----
    python extract_manifestos.py            # all parties
    python extract_manifestos.py ÖVP SPÖ    # specific parties
"""

import re
import statistics
import sys
from collections import Counter
from pathlib import Path

import pymupdf

MANIFESTO_DIR = Path(__file__).parent / "manifestos"

PARTIES = {
    "ÖVP":   ("oevp.pdf",   "oevp_clean.txt"),
    "SPÖ":   ("spoe.pdf",   "spoe_clean.txt"),
    "FPÖ":   ("fpoe.pdf",   "fpoe_clean.txt"),
    "GRÜNE": ("gruene.pdf", "gruene_clean.txt"),
    "NEOS":  ("neos.pdf",   "neos_clean.txt"),
}

TOP_MARGIN    = 0.055   # share of page height treated as running header zone
BOTTOM_MARGIN = 0.94    # below this: footer / page numbers
MAX_FONT      = 40      # larger = decorative lettering, not text
H1_RATIO      = 1.8
H2_RATIO      = 1.12
MAX_HEADING_CHARS = 150  # longer "headings" are lead paragraphs set in a big font

# Documents where "bigger than body" isn't a reliable heading signal: list the
# exact (level, size, font) used for headings. All other big text is treated as
# paragraph text (lead paragraphs, key-measure boxes).
HEADING_FONTS = {
    "ÖVP": [("h1", 48.0, "Geomanist-Medium"),
            ("h1", 26.0, "GeomanistMedium-Italic"),
            ("h2", 15.0, "Geomanist-Medium")],
}

_CONJ = re.compile(r'(und|oder|bzw\.?|sowie|bis|als|wie)\b')
_BULLET = re.compile(r'^[•●▪■\-–—✓→]\s')
_SENTENCE_END = re.compile(r'[.!?:;“"»)]\s*$')


# ── Reading the PDF ───────────────────────────────────────────────────────────

def read_lines(doc) -> list[dict]:
    """All text lines with page, position, size and boldness."""
    out = []
    for pno, page in enumerate(doc):
        W, H = page.rect.width, page.rect.height
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                spans = [s for s in l["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = "".join(s["text"] for s in l["spans"])
                text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)   # stray control chars from the PDF
                text = re.sub(r'[ \t ]+', ' ', text).strip()
                big = max(spans, key=lambda s: s["size"])
                size = big["size"]
                x0, y0, x1, y1 = l["bbox"]
                out.append({
                    "page": pno, "W": W, "H": H, "text": text,
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "size": round(size, 1), "font": big["font"],
                    "bold": all(s["flags"] & 16 for s in spans),
                })
    return out


def body_size(lines: list[dict]) -> float:
    c = Counter()
    for l in lines:
        c[l["size"]] += len(l["text"])
    return c.most_common(1)[0][0]


def column_starts(page_lines: list[dict], body: float, W: float) -> list[float]:
    """Left edges of the text columns on a page, from where body lines start."""
    xs = sorted(l["x0"] for l in page_lines if abs(l["size"] - body) <= 1.0)
    if not xs:
        return [0.0]
    clusters, cur = [], [xs[0]]
    for x in xs[1:]:
        if x - cur[-1] > 30:
            clusters.append(cur)
            cur = [x]
        else:
            cur.append(x)
    clusters.append(cur)
    starts = [min(c) for c in clusters if len(c) >= 3]
    # columns must be clearly apart (at least ~20 % of the page width)
    merged = []
    for s in starts:
        if merged and s - merged[-1] < W * 0.2:
            continue
        merged.append(s)
    return merged or [xs[0]]


def reading_order(lines: list[dict], body: float) -> list[dict]:
    """Keep real text lines and sort them page by page, column by column."""
    ordered = []
    pages = sorted({l["page"] for l in lines})
    for p in pages:
        pl = [l for l in lines if l["page"] == p]
        W, H = pl[0]["W"], pl[0]["H"]
        pl = [l for l in pl
              if l["y0"] >= H * TOP_MARGIN and l["y1"] <= H * BOTTOM_MARGIN
              and l["size"] <= MAX_FONT and l["x0"] >= -2 and l["x1"] <= W + 2
              and re.search(r'[A-Za-zÄÖÜäöüß]{3,}', l["text"])]
        if not pl:
            continue
        cols = column_starts(pl, body, W)
        for l in pl:
            if l["size"] >= body * H1_RATIO:
                l["col"] = 0                     # page titles come first
            else:
                l["col"] = max([i for i, s in enumerate(cols) if l["x0"] >= s - 12] or [0])
        pl.sort(key=lambda l: (l["col"], round(l["y0"]), l["x0"]))
        ordered.extend(pl)
    return ordered


# ── Building headings and paragraphs ─────────────────────────────────────────

def join_lines(a: str, b: str) -> str:
    """Join two wrapped lines, repairing hyphenation."""
    if a.endswith("­"):                       # soft hyphen: always a syllable break
        return a[:-1] + b
    if re.search(r'\w-$', a):
        if _CONJ.match(b):                         # "Wirtschafts-" + "und …"
            return a + " " + b
        if b[:1].islower():                        # "Technologie-" + "offenheit"
            return a[:-1] + b
        return a + b                               # "Corona-" + "Krise"
    return a + " " + b


def heading_level(l: dict, body: float, fonts) -> str | None:
    if fonts is not None:
        for level, size, font in fonts:
            if abs(l["size"] - size) <= 0.3 and l["font"] == font:
                return level
        return None
    return ("h1" if l["size"] >= body * H1_RATIO else
            "h2" if l["size"] >= body * H2_RATIO else None)


def structure(lines: list[dict], body: float, fonts=None) -> list[tuple[str, str]]:
    """Turn ordered lines into [("h1"|"h2"|"p", text), …]."""
    out: list[tuple[str, str]] = []
    para, prev = None, None
    col_width: dict[tuple, float] = {}
    for l in lines:
        if abs(l["size"] - body) <= 1.0:
            key = (l["page"], l["col"])
            col_width[key] = max(col_width.get(key, 0), l["x1"] - l["x0"])

    def flush():
        nonlocal para
        if para:
            out.append(("p", para.strip()))
        para = None

    heading = None   # (level, size, page, text)

    def flush_heading():
        nonlocal heading
        if heading:
            level, _, _, text = heading
            if len(text) > MAX_HEADING_CHARS:
                out.append(("p", text))            # big-font lead paragraph, not a heading
            else:
                out.append((level, text))
        heading = None

    for l in lines:
        size, text = l["size"], l["text"]
        level = heading_level(l, body, fonts)

        if level:
            flush()
            if (heading and heading[0] == level and heading[2] == l["page"]
                    and abs(heading[1] - size) < 0.6):
                heading = (level, size, l["page"], join_lines(heading[3], text))
            else:
                flush_heading()
                heading = (level, size, l["page"], text)
            prev = None
            continue

        flush_heading()
        new_para = para is None or _BULLET.match(text)
        if not new_para and prev is not None:
            same_col = prev["page"] == l["page"] and prev["col"] == l["col"]
            gap = l["y0"] - prev["y1"]
            width = col_width.get((prev["page"], prev["col"]), 0)
            short_last = width and (prev["x1"] - prev["x0"]) < width * 0.75
            ended = bool(_SENTENCE_END.search(prev["text"]))
            if abs(l["size"] - prev["size"]) > 1.0:
                new_para = True                    # different text style (e.g. lead text)
            elif same_col and gap > prev["size"] * 0.9:
                new_para = True                    # visible paragraph spacing
            elif ended and short_last:
                new_para = True                    # short last line of a paragraph
            elif not same_col and ended:
                new_para = True                    # column/page break after a full sentence
        if new_para:
            flush()
            para = text
        else:
            para = join_lines(para, text)
        prev = l

    flush()
    flush_heading()
    return out


def render(blocks: list[tuple[str, str]]) -> str:
    parts = []
    for kind, text in blocks:
        text = text.replace("­", "")
        if kind == "h1":
            parts.append(f"# {text}")
        elif kind == "h2":
            parts.append(f"## {text}")
        else:
            parts.append(text)
    return "\n\n".join(parts) + "\n"


def process(party: str) -> None:
    src_name, dst_name = PARTIES[party]
    doc = pymupdf.open(MANIFESTO_DIR / src_name)
    lines = read_lines(doc)
    body = body_size(lines)
    blocks = structure(reading_order(lines, body), body, HEADING_FONTS.get(party))
    (MANIFESTO_DIR / dst_name).write_text(render(blocks), encoding="utf-8")
    kinds = Counter(k for k, _ in blocks)
    print(f"[{party}] body {body} pt · {kinds['h1']} H1 · {kinds['h2']} H2 · "
          f"{kinds['p']} paragraphs → {dst_name}")


if __name__ == "__main__":
    for p in sys.argv[1:] or list(PARTIES):
        if p not in PARTIES:
            print(f"Unknown party: {p}. Valid: {list(PARTIES)}")
            continue
        process(p)
