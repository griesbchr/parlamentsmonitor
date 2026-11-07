"""
export_static.py – Export the dashboard as a static site for GitHub Pages.

The app only reads data, so every API response can be written out as a JSON
file ahead of time. The Flask endpoints are called through the test client,
so the static site and the local app always show exactly the same data.

Output (default: ./site, its own git repo for the public Pages repository):
    site/index.html              the dashboard, in static mode
    site/data/items.json         list of all decisions          (= /api/items)
    site/data/tagungen.json      sessions for the filter        (= /api/tagungen)
    site/data/items/<id>.json    {"item": ..., "analyses": ...} (= /api/items/<id>[/analyses])
    site/data/themen.json        topic list                     (= /api/themen)
    site/data/themen/<slug>.json manifesto passages per topic   (= /api/themen/<slug>)
    site/og.png                  link-preview image

Law documents are linked to parlament.gv.at instead of being copied.

Usage
-----
    python export_static.py               # export to ./site
    python export_static.py --push        # export, then commit + push ./site if it has a remote
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app import app

ROOT = Path(__file__).parent
OUT  = ROOT / "site"
SITE_URL = "https://griesbchr.github.io/parlamentsmonitor/"

# Visitor statistics: set to your GoatCounter site code (https://<code>.goatcounter.com)
# to enable cookie-free page-view counting. Empty = no statistics script at all.
# The Impressum's privacy section adapts automatically.
GOATCOUNTER_CODE = ""

PARTIES = ["ÖVP", "SPÖ", "FPÖ", "GRÜNE", "NEOS"]
PARTY_COLORS = {"ÖVP": "#62C3D0", "SPÖ": "#E3000F", "FPÖ": "#0052FB",
                "GRÜNE": "#57A12B", "NEOS": "#E3257B"}


def contradiction_counts(items: list[dict]) -> dict[str, int]:
    return {p: sum(1 for it in items if (it.get("scores") or {}).get(p, 0) <= -1)
            for p in PARTIES}


def make_og_image(items: list[dict], path: Path) -> bool:
    """1200x630 link-preview image with the contradiction count per party."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Pillow not installed – skipping og.png")
        return False

    def font(bold: bool, size: int):
        for name in (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold
                     else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), "#1a1a2e")
    d = ImageDraw.Draw(img)
    d.text((70, 60), "Parlamentsmonitor", font=font(True, 64), fill="white")
    d.text((70, 145), "Stimmen Österreichs Parteien so ab,", font=font(False, 36), fill="#c7cad6")
    d.text((70, 190), "wie sie es im Wahlprogramm versprochen haben?", font=font(False, 36), fill="#c7cad6")

    counts = contradiction_counts(items)
    top = max(counts.values()) or 1
    d.text((70, 275), "Widersprüche zum eigenen Wahlprogramm (KI-Einschätzung)",
           font=font(False, 26), fill="#8a919c")
    y = 325
    for p in sorted(PARTIES, key=lambda p: -counts[p]):
        d.text((70, y), p, font=font(True, 30), fill="white")
        bar = int(760 * counts[p] / top)
        d.rounded_rectangle((230, y + 6, 230 + max(bar, 6), y + 34), radius=6, fill=PARTY_COLORS[p])
        d.text((245 + max(bar, 6), y), str(counts[p]), font=font(True, 30), fill="white")
        y += 52
    img.save(path, "PNG", optimize=True)
    return True


def head_tags(items: list[dict], has_image: bool) -> str:
    counts = contradiction_counts(items)
    summary = ", ".join(f"{p} {n}" for p, n in sorted(counts.items(), key=lambda x: -x[1]))
    desc = (f"{len(items)} Beschlüsse des Nationalrats mit KI-Abgleich zum Wahlprogramm. "
            f"Widersprüche: {summary}.")
    tags = [
        '<meta name="static-site" content="1">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:url" content="{SITE_URL}">',
        '<meta property="og:title" content="Parlamentsmonitor">',
        f'<meta property="og:description" content="{desc}">',
        '<meta name="twitter:card" content="summary_large_image">',
    ]
    if has_image:
        tags.append(f'<meta property="og:image" content="{SITE_URL}og.png">')
    if GOATCOUNTER_CODE:
        # no_onload: the page counts each #/route itself (countPageView in index.html)
        tags.append(f'<script data-goatcounter="https://{GOATCOUNTER_CODE}.goatcounter.com/count" '
                    f'data-goatcounter-settings=\'{{"no_onload": true}}\' async '
                    f'src="https://gc.zgo.at/count.js"></script>')
    return "\n  ".join(tags)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")


def get(client, url: str):
    r = client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"{url} -> HTTP {r.status_code}")
    return r


def export(out: Path = OUT) -> int:
    client = app.test_client()
    out.mkdir(exist_ok=True)

    items = get(client, "/api/items").get_json()

    # index.html in static mode, with link-preview tags
    has_image = make_og_image(items, out / "og.png")
    page = get(client, "/").get_data(as_text=True)
    page = page.replace("<head>", "<head>\n  " + head_tags(items, has_image), 1)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / ".nojekyll").write_text("")   # serve files as-is, no Jekyll processing

    # data (rebuild from scratch so removed items don't linger)
    data_dir = out / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)

    write_json(data_dir / "items.json", items)
    write_json(data_dir / "tagungen.json", get(client, "/api/tagungen").get_json())

    topics = get(client, "/api/themen").get_json()
    write_json(data_dir / "themen.json", topics)
    for t in topics:
        write_json(data_dir / "themen" / f"{t['slug']}.json",
                   get(client, f"/api/themen/{t['slug']}").get_json())

    for it in items:
        write_json(data_dir / "items" / f"{it['id']}.json", {
            "item":     get(client, f"/api/items/{it['id']}").get_json(),
            "analyses": get(client, f"/api/items/{it['id']}/analyses").get_json(),
        })

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file() and ".git" not in f.parts)
    print(f"Exported {len(items)} items to {out}  ({size / 1e6:.1f} MB)")
    return len(items)


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=OUT, capture_output=True, text=True, check=check)


def analyses_complete(min_share: float = 0.9) -> bool:
    """
    False while a re-score is running (--reset clears vote_analyses until the
    batch results arrive): never publish a site without its assessments.
    """
    import database as db
    conn = db.connect()
    expected = conn.execute(
        "SELECT COUNT(*) FROM votes WHERE club_code IN ('V','S','F','G','N')").fetchone()[0]
    have = conn.execute("SELECT COUNT(*) FROM vote_analyses").fetchone()[0]
    conn.close()
    if expected and have < expected * min_share:
        print(f"Only {have} of {expected} votes are assessed (re-score running?) – not pushing.")
        return False
    return True


def push() -> None:
    """Commit and push ./site, if it is a git repo with a remote."""
    if not analyses_complete():
        sys.exit(1)
    if not (OUT / ".git").exists():
        print("site/ is not a git repository yet – skipping push.")
        return
    if not git("remote", check=False).stdout.strip():
        print("site/ has no git remote – skipping push.")
        return
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("Site unchanged – nothing to push.")
        return
    git("commit", "-m", f"Update data {datetime.now():%Y-%m-%d}")
    result = git("push", check=False)
    if result.returncode != 0:
        print("ERROR: git push failed:\n" + result.stderr, file=sys.stderr)
        sys.exit(1)
    print("Site pushed.")


if __name__ == "__main__":
    export()
    if "--push" in sys.argv:
        push()
