"""
extract_promises_rules.py – Rule-based promise extraction from manifesto text files.
Party-specific extractors tailored to each document's actual structure.

Run:  python extract_promises_rules.py
Re-run:  python extract_promises_rules.py --reset
"""
import re
import sys
import logging
from pathlib import Path
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent
MANIFESTO_DIR = BASE_DIR / "manifestos"

# ── Policy area detection ─────────────────────────────────────────────────────
AREA_MAP = {
    "Wirtschaft":     ["wirtschaft","betrieb","unternehm","kmu","industrie","export","innovation",
                       "standort","wettbewerb","wachstum","konjunktur","förder"],
    "Steuern":        ["steuer","abgabe","entlastung","lohnnebenkosten","körperschaft",
                       "mehrwertsteuer","kapitalertrag","erbschaft","einkommensteuer","abgabenquote"],
    "Arbeit":         ["arbeit","beschäftig","arbeitslos","vollzeit","teilzeit","überstund",
                       "lohn","mindestlohn","kollektivvertrag","arbeitszeit","arbeitsmarkt","fachkraft"],
    "Wohnen":         ["wohn","miete","mietpreis","bauland","wohnbau","eigenheim","wohnkosten",
                       "immobilien","grundstück","mietendeckel"],
    "Gesundheit":     ["gesundheit","arzt","ärztin","krankenhaus","kassenarzt","pflege",
                       "medizin","spital","krankenversicherung"],
    "Bildung":        ["bildung","schule","universität","hochschule","ausbildung","lehrerin",
                       "lehrer","kindergarten","studium","bildungs","lehre"],
    "Klimaschutz":    ["klima","co2","erneuerbar","emission","fossil","nachhaltigkeit","klimawandel",
                       "solar","wind","klimaschutz","dekarbonisierung"],
    "Energie":        ["energie","strom","gas","heizung","elektrizität","energiepreis",
                       "energiewende","versorgungssicherheit"],
    "Migration":      ["asyl","migration","migrant","flüchtling","zuwanderung","abschieb",
                       "integration","staatsbürger"],
    "Sicherheit":     ["polizei","kriminalität","heer","bundesheer","verteidigung",
                       "innere sicherheit","terror","cybersicherheit"],
    "Soziales":       ["sozial","armut","sozialhilfe","grundsicherung","mindestsicherung",
                       "sozialsystem","behinderung","inklusion"],
    "Pension":        ["pension","rente","pensionistin","pensionsalter","altersvorsorge","pensionssystem"],
    "Demokratie":     ["demokratie","rechtsstaat","transparenz","korruption","verfassung",
                       "parlament","wahlrecht","direkte demokratie","volksabstimmung"],
    "Frauen":         ["frauen","gleichstellung","lohngleichheit","gewalt gegen frauen",
                       "karenz","frauenpolitik"],
    "Landwirtschaft": ["landwirtschaft","bauer","bauern","agrar","lebensmittel","bio",
                       "tierwohl","pestizid"],
    "Familie":        ["familie","kind","kinder","kindergeld","familienbonus","betreuung",
                       "eltern","geburt"],
    "Verkehr":        ["verkehr","straße","bahn","öffis","mobilität","flughafen","fahrrad",
                       "autobahn","schiene"],
    "Digitalisierung":["digital","künstliche intelligenz"," ki ","internet","breitband",
                       "e-government","datenschutz"],
    "Europa":         ["europa"," eu ","europäisch","nato","außenpolitik","international"],
    "Medien":         ["medien","orf","presse","rundfunk","journalismus"],
}

def detect_area(text: str) -> str:
    t = text.lower()
    scores = {area: sum(1 for kw in kws if kw in t) for area, kws in AREA_MAP.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Sonstiges"

def classify_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["fordern","verlangen","brauchen wir","müssen wir"]):
        return "Forderung"
    if any(w in t for w in ["ziel","wollen","streben","anstreben","beabsichtigen","soll"]):
        return "Ziel"
    return "Vorhaben"

def specificity(text: str) -> int:
    s = 2
    if re.search(r'\d', text): s += 1
    if re.search(r'%|Prozent|Euro|€|Mrd\.|Mio\.', text): s += 1
    return min(s, 5)

def make_promise(party: str, text: str, src: str = "") -> dict:
    text = re.sub(r'\s+', ' ', text).strip()
    return {
        "party": party, "promise_text": text,
        "policy_area": detect_area(text), "promise_type": classify_type(text),
        "specificity": specificity(text), "source_pages": src
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NEOS – "• Title – description" bullet points after "Unsere Reformen für..."
# ═══════════════════════════════════════════════════════════════════════════════
def extract_neos(text: str) -> list[dict]:
    results = []
    seen = set()

    # Bullet points: "•\t Title – description" (tab-separated in PDF text)
    bullet_pat = re.compile(
        r'•\s+([A-ZÄÖÜ][^\n•]{15,350})',
        re.MULTILINE
    )
    for m in bullet_pat.finditer(text):
        raw = m.group(1).strip()
        # Join wrapped lines (lines not starting with capital after newline)
        raw = re.sub(r'\n\s+', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
        # Remove trailing section labels in ALLCAPS
        raw = re.sub(r'\s+[A-ZÄÖÜ]{4,}$', '', raw).strip()
        if len(raw) < 25 or raw in seen:
            continue
        seen.add(raw)
        results.append(make_promise("NEOS", raw))

    log.info(f"[NEOS] {len(results)} promises from bullets")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# FPÖ – CAPS headings with following description paragraph
# ═══════════════════════════════════════════════════════════════════════════════
def extract_fpoe(text: str) -> list[dict]:
    results = []
    seen = set()

    # Pattern: indented CAPS heading followed by description
    # " VOLKSINITIATIVE EINFÜHREN\n  description..."
    caps_bullet = re.compile(
        r'[ \t]{1,4}([A-ZÄÖÜ][A-ZÄÖÜ\s\-]{8,80})\n[ \t]{2,}([^\n]{30,500})',
        re.MULTILINE
    )
    for m in caps_bullet.finditer(text):
        heading = m.group(1).strip()
        body = m.group(2).strip()
        # Skip pure section headers
        if len(heading.split()) > 10:
            continue
        combined = f"{heading.title()}: {body}"
        combined = re.sub(r'\s+', ' ', combined).strip()
        if combined not in seen and len(combined) > 40:
            seen.add(combined)
            results.append(make_promise("FPÖ", combined))

    # Also "wir wollen/fordern/werden" sentences
    wir_pat = re.compile(
        r'[Ww]ir\s+(?:wollen|fordern|werden|setzen|schaffen|lehnen|bekennen|führen)[^.!?]{25,400}[.!?]',
        re.MULTILINE
    )
    for m in wir_pat.finditer(text):
        s = re.sub(r'\s+', ' ', m.group()).strip()
        if s not in seen and len(s) > 40:
            seen.add(s)
            results.append(make_promise("FPÖ", s))

    log.info(f"[FPÖ] {len(results)} promises")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# SPÖ – bullet-point content within "UNSERE IDEEN FÜR..." sections
#        plus explicitly labeled "SPÖ-FORDERUNGEN" paragraphs
# ═══════════════════════════════════════════════════════════════════════════════
def extract_spoe(text: str) -> list[dict]:
    results = []
    seen = set()

    # "UNSERE IDEEN FÜR X\n ... content until next section"
    section_pat = re.compile(
        r'UNSERE IDEEN FÜR[^\n]*\n(.*?)(?=UNSERE IDEEN FÜR|\[S\.\d+\]|\Z)',
        re.DOTALL | re.IGNORECASE
    )

    for sec in section_pat.finditer(text):
        content = sec.group(1)
        section_area = detect_area(sec.group(0)[:200])

        # Extract sub-headings + following paragraph
        # Pattern: short non-lowercase line (heading), followed by text
        lines = [l.strip() for l in content.split('\n')]
        heading = ""
        body_lines = []
        for line in lines:
            if not line:
                if heading and body_lines:
                    body = ' '.join(body_lines)
                    promise = f"{heading}: {body}" if body else heading
                    promise = re.sub(r'\s+', ' ', promise).strip()
                    if 40 < len(promise) < 500 and promise not in seen:
                        seen.add(promise)
                        p = make_promise("SPÖ", promise)
                        if p["policy_area"] == "Sonstiges":
                            p["policy_area"] = section_area
                        results.append(p)
                heading = ""
                body_lines = []
                continue

            # Heading: short, no period, starts with uppercase
            if (len(line) < 80 and not line.endswith('.')
                    and line and line[0].isupper()
                    and not line.startswith('[S.')):
                if heading and body_lines:
                    body = ' '.join(body_lines)
                    promise = f"{heading}: {body}" if body else heading
                    promise = re.sub(r'\s+', ' ', promise).strip()
                    if 40 < len(promise) < 500 and promise not in seen:
                        seen.add(promise)
                        p = make_promise("SPÖ", promise)
                        if p["policy_area"] == "Sonstiges":
                            p["policy_area"] = section_area
                        results.append(p)
                heading = line
                body_lines = []
            else:
                body_lines.append(line)

    # Also "wir fordern/wollen" sentences throughout
    wir_pat = re.compile(
        r'[Ww]ir\s+(?:fordern|wollen|streben|werden|schaffen|führen)\s[^.!?]{25,350}[.!?]',
        re.MULTILINE
    )
    for m in wir_pat.finditer(text):
        s = re.sub(r'\s+', ' ', m.group()).strip()
        if s not in seen and len(s) > 40:
            seen.add(s)
            results.append(make_promise("SPÖ", s))

    log.info(f"[SPÖ] {len(results)} promises")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# GRÜNE – "wir" commitment sentences; chapter bullets
# ═══════════════════════════════════════════════════════════════════════════════
def extract_gruene(text: str) -> list[dict]:
    results = []
    seen = set()

    # "wir X" commitment sentences
    wir_pat = re.compile(
        r'[Ww]ir\s+(?:wollen|werden|fordern|setzen|schaffen|investieren|stärken|einführen|'
        r'ausbauen|ermöglichen|sichern|lehnen|verbessern|erhöhen|senken|streichen|abschaffen|'
        r'bekennen|unterstützen|brauchen|schaffen)[^.!?]{20,400}[.!?]',
        re.MULTILINE
    )
    for m in wir_pat.finditer(text):
        s = re.sub(r'\s+', ' ', m.group()).strip()
        if s not in seen and 40 < len(s) < 450:
            seen.add(s)
            results.append(make_promise("GRÜNE", s))

    # Numbered/bulleted demands
    bullet_pat = re.compile(
        r'(?:^|\n)(?:\d+\.|[•·▶►])\s*([A-ZÄÖÜ][^\n•·▶►\d\.]{25,300})',
        re.MULTILINE
    )
    for m in bullet_pat.finditer(text):
        s = re.sub(r'\s+', ' ', m.group(1)).strip()
        if s not in seen and len(s) > 40:
            seen.add(s)
            results.append(make_promise("GRÜNE", s))

    log.info(f"[GRÜNE] {len(results)} promises")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# ÖVP – bullet points and "wir" sentences; also key manually-curated promises
# ═══════════════════════════════════════════════════════════════════════════════
OEVP_MANUAL = [
    # Steuern
    ("Steuern", "Vorhaben", 5, "Senkung des Eingangssteuersatzes von 20% auf 15% für fünf Millionen Steuerzahler."),
    ("Steuern", "Vorhaben", 5, "Abschaffung der Steuerstufe von 48 Prozent."),
    ("Steuern", "Vorhaben", 5, "Einführung eines jährlichen steuerlichen Vollzeitbonus in Höhe von 1.000 Euro für Vollzeitarbeit."),
    ("Steuern", "Vorhaben", 5, "Alle Überstunden zur Gänze steuerfrei machen."),
    ("Steuern", "Vorhaben", 4, "Lohnnebenkosten-Senkungspfad um 0,5 Prozentpunkte pro Jahr bis 2030."),
    ("Steuern", "Vorhaben", 5, "Kein Kranken- und Pensionsversicherungsbeitrag für Arbeiten nach dem gesetzlichen Pensionsalter."),
    ("Steuern", "Vorhaben", 4, "Klares Nein zu neuen Vermögens- und Erbschaftssteuern."),
    ("Steuern", "Vorhaben", 5, "Versicherungssteuer für Pensionskassenbeiträge von 2,5% auf 1,5% senken."),
    ("Steuern", "Vorhaben", 5, "Freibetrag für Zukunftssicherung von 300 Euro auf 1.200 Euro anheben."),
    ("Steuern", "Vorhaben", 5, "Veranlagungsfreibetrag von 730 Euro auf 1.500 Euro erhöhen."),
    ("Wirtschaft", "Vorhaben", 5, "Abgabenquote in Richtung 40 Prozent senken."),
    ("Wirtschaft", "Vorhaben", 4, "Neues Standort-Förderungsgesetz schaffen, das sicherstellt, dass die Körperschaftsteuer immer unter EU-Durchschnitt liegt."),
    ("Wirtschaft", "Vorhaben", 4, "Grenze für geringwertige Wirtschaftsgüter von 1.000 auf 2.500 Euro erhöhen und degressive Abschreibung ausbauen."),
    ("Wirtschaft", "Vorhaben", 4, "Berichtspflichten um ein Drittel reduzieren und Gold Plating konsequent verhindern."),
    ("Wirtschaft", "Vorhaben", 3, "Gesetze mit Ablaufdatum versehen ('Sunset Clauses') und 'One in – two out'-Prinzip für neue Regulierungen einführen."),
    ("Wirtschaft", "Vorhaben", 4, "Strompreiskompensation für die Industrie bis 2030 verlängern."),
    ("Haushalt", "Vorhaben", 5, "Bis Ende der Legislaturperiode zu einem ausgeglichenen Budget zurückfinden, dann keine neuen Schulden."),
    ("Arbeit", "Vorhaben", 4, "Arbeitslosenversicherung NEU mit degressive Arbeitslosengeld und Wartefrist einführen."),
    # Gesundheit
    ("Gesundheit", "Vorhaben", 5, "800 neue Kassenarzt-Stellen schaffen."),
    ("Gesundheit", "Vorhaben", 4, "Berufspflicht für Ärztinnen und Ärzte nach der Ausbildung in Österreich einführen."),
    ("Gesundheit", "Vorhaben", 5, "11 Milliarden Euro in Pflege- und Gesundheitssystem investieren."),
    # Wohnen
    ("Wohnen", "Vorhaben", 4, "Grunderwerbssteuer für den Erstwohnsitzkauf abschaffen."),
    ("Wohnen", "Vorhaben", 3, "Günstige Kreditprogramme und Eigenmittelersatzdarlehen für Wohneigentum schaffen."),
    ("Wohnen", "Vorhaben", 3, "Verlängerung der KIM-Verordnung ablehnen."),
    # Familie
    ("Familie", "Vorhaben", 4, "Historische Kinderbetreuungsinitiative: echte Wahlfreiheit in der Kinderbetreuung schaffen."),
    ("Familie", "Vorhaben", 3, "Babyprodukte steuerlich absetzbar machen."),
    # Bildung
    ("Bildung", "Vorhaben", 4, "Leistungsgruppen in Schulen wieder einführen."),
    ("Bildung", "Vorhaben", 4, "Mehr MINT- und FH-Studienplätze schaffen."),
    ("Bildung", "Vorhaben", 3, "Vorbereitungskurskosten für Meister- und Befähigungsprüfungen abschaffen."),
    ("Bildung", "Vorhaben", 4, "Hormonbehandlungen unter 18 Jahren ohne medizinische Gründe verbieten."),
    # Migration
    ("Migration", "Vorhaben", 4, "Verfahren im Ausland für Asylwerber schaffen und straffällige Asylwerber konsequent abschieben."),
    ("Migration", "Vorhaben", 4, "Sachleistungen statt Geldleistungen für Asylwerber (Bezahlkarte einführen)."),
    ("Migration", "Vorhaben", 4, "Familiennachzug temporär aussetzen."),
    ("Migration", "Vorhaben", 4, "Verpflichtende Sprachstandsfeststellung vor dem Regelschulbetrieb einführen."),
    ("Migration", "Vorhaben", 4, "Staatsbürgerschaft nur mit verpflichtender Absolvierung von Staatsbürgerschaftskursen und Deutschprüfungen vergeben."),
    # Sicherheit
    ("Sicherheit", "Vorhaben", 5, "32.000 mobile Polizeiinspektionen schaffen."),
    ("Sicherheit", "Vorhaben", 5, "Budget des Bundesheers langfristig auf 2% des BIP steigern."),
    # Gesellschaft
    ("Demokratie", "Vorhaben", 4, "Shoah-Zentrum und Holocaust-Museum errichten."),
    ("Demokratie", "Vorhaben", 3, "Bargeldversorgung flächendeckend sichern, gegen Abschaffung des Bargelds."),
    ("Demokratie", "Vorhaben", 3, "ORF reformieren: schlanker, bürgernäher und regionaler."),
    # Klima/Energie
    ("Klimaschutz", "Vorhaben", 3, "Zu den Pariser Klimazielen bekennen, CO₂ sicher speichern (CCS)."),
    ("Energie", "Vorhaben", 4, "Erneuerbare Energien durch Beschleunigung der Genehmigungsverfahren schneller ausbauen."),
    # Digitalisierung
    ("Digitalisierung", "Vorhaben", 4, "Digital Austria Act umsetzen und KI-Anwendungen im öffentlichen Dienst einführen."),
    # Infrastruktur
    ("Verkehr", "Vorhaben", 3, "Neue Straßen und Verkehrswege bauen, regionale Flughäfen sichern."),
    ("Landwirtschaft", "Vorhaben", 3, "Wasserversorgung vor Privatisierung schützen."),
]

def extract_oevp(text: str) -> list[dict]:
    results = []
    seen = set()

    # Insert manually-curated high-quality promises
    for area, ptype, spec, txt in OEVP_MANUAL:
        results.append({
            "party": "ÖVP", "promise_text": txt, "policy_area": area,
            "promise_type": ptype, "specificity": spec, "source_pages": "3-39",
        })
        seen.add(txt)

    # Also extract "wir" sentences from text as supplemental
    wir_pat = re.compile(
        r'[Ww]ir\s+(?:wollen|werden|führen[^.]{0,30}ein|schaffen|setzen|bekennen|senken|erhöhen|'
        r'stärken|abschaffen|investieren|ermöglichen|bauen|lehnen)[^.!?]{25,350}[.!?]',
        re.MULTILINE
    )
    for m in wir_pat.finditer(text):
        s = re.sub(r'\s+', ' ', m.group()).strip()
        # Skip abstract/rhetorical sentences
        skip_words = ["wer ", "festung", "ideologie", "system", "mitte", "land ist",
                      "unsere werte", "unsere heimat"]
        if any(w in s.lower() for w in skip_words):
            continue
        if s not in seen and 50 < len(s) < 400:
            seen.add(s)
            results.append(make_promise("ÖVP", s))

    log.info(f"[ÖVP] {len(results)} promises")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
EXTRACTORS = {
    "ÖVP":   ("oevp_text.txt",   extract_oevp),
    "SPÖ":   ("spoe_text.txt",   extract_spoe),
    "FPÖ":   ("fpoe_text.txt",   extract_fpoe),
    "GRÜNE": ("gruene_text.txt", extract_gruene),
    "NEOS":  ("neos_text.txt",   extract_neos),
}

def run(reset: bool = False):
    conn = db.connect()
    db.init_schema(conn)

    for party, (filename, extractor) in EXTRACTORS.items():
        if reset:
            conn.execute("DELETE FROM manifesto_promises WHERE party=?", (party,))
            conn.commit()

        existing = conn.execute(
            "SELECT COUNT(*) FROM manifesto_promises WHERE party=?", (party,)
        ).fetchone()[0]
        if existing > 0:
            log.info(f"[{party}] Already has {existing} promises – skipping (use --reset to redo)")
            continue

        fp = MANIFESTO_DIR / filename
        if not fp.exists():
            log.error(f"[{party}] Missing: {fp}")
            continue

        text = fp.read_text(encoding="utf-8")
        promises = extractor(text)

        # Deduplicate and filter
        seen = set()
        unique = []
        for p in promises:
            t = p["promise_text"].strip()
            # Skip empty, too short, or pure section titles
            if not t or len(t) < 35:
                continue
            # Skip if it's just a headline without a verb
            # NEOS/FPÖ use title-style bullets so skip the verb filter for them
            if party not in ("ÖVP", "NEOS", "FPÖ"):
                if not re.search(r'(?:wir|wollen|werden|fordern|führen|schaffen|einführen|soll|'
                                 r'muss|streben|bekennen|stärken|senken|erhöhen|abschaffen|bauen|'
                                 r'ermöglichen|investieren|verbieten|sichern|lehnen|reformieren|'
                                 r'verbessern|ausbauen|deckel|reduzi)', t, re.I):
                    continue
            if t in seen:
                continue
            seen.add(t)
            unique.append(p)

        conn.executemany(
            """INSERT INTO manifesto_promises
               (party, promise_text, policy_area, promise_type, specificity, source_pages)
               VALUES (:party, :promise_text, :policy_area, :promise_type, :specificity, :source_pages)""",
            unique,
        )
        conn.commit()
        log.info(f"[{party}] {len(unique)} promises stored")

    # Summary
    print("\n=== Versprechen-Übersicht ===")
    for row in conn.execute(
        "SELECT party, COUNT(*) as n FROM manifesto_promises GROUP BY party ORDER BY party"
    ).fetchall():
        areas = conn.execute("""
            SELECT policy_area, COUNT(*) as n FROM manifesto_promises
            WHERE party=? GROUP BY policy_area ORDER BY n DESC LIMIT 4
        """, (row["party"],)).fetchall()
        area_str = ", ".join(f"{a['policy_area']}({a['n']})" for a in areas)
        print(f"  {row['party']:<8}  {row['n']:>4}  |  {area_str}")
    conn.close()

if __name__ == "__main__":
    reset = "--reset" in sys.argv
    run(reset)
