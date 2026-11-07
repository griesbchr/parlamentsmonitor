"""
embeddings.py – Semantic matching between votes and manifesto sections.

Uses a small multilingual embedding model that runs locally via fastembed
(ONNX, no PyTorch). Manifesto sections are embedded once and stored in the
section_embeddings table; each vote is embedded on the fly from its title,
description and the start of the law text.

The e5 models expect the prefixes "query: " and "passage: ".

Only available inside the project's .venv (see README). If fastembed is not
installed, available() returns False and callers fall back to keyword search.
"""

import logging

import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "intfloat/multilingual-e5-small"
MAX_CHARS = 1800          # ~512 tokens, the model's input limit

_model = None
_party_cache: dict[str, tuple[list[int], np.ndarray]] = {}


def available() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType
        # multilingual-e5-small isn't built into fastembed; register the ONNX export
        # from its Hugging Face repository (downloaded once, ~470 MB, then cached).
        if MODEL_NAME not in [m["model"] for m in TextEmbedding.list_supported_models()]:
            TextEmbedding.add_custom_model(
                model=MODEL_NAME,
                pooling=PoolingType.MEAN,
                normalization=True,
                sources=ModelSource(hf=MODEL_NAME),
                dim=384,
                model_file="onnx/model.onnx",
            )
        log.info(f"Loading embedding model {MODEL_NAME} …")
        _model = TextEmbedding(MODEL_NAME)
    return _model


def _embed(texts: list[str]) -> np.ndarray:
    vecs = np.array(list(_get_model().embed(texts)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    return vecs


def ensure_section_embeddings(conn) -> int:
    """Embed all manifesto sections that have no vector yet. Returns how many were added."""
    rows = conn.execute("""
        SELECT s.id, s.section_path, s.text FROM manifesto_sections s
        LEFT JOIN section_embeddings e ON e.section_id = s.id
        WHERE e.section_id IS NULL
    """).fetchall()
    # drop vectors of sections that no longer exist (after a re-index)
    conn.execute("DELETE FROM section_embeddings "
                 "WHERE section_id NOT IN (SELECT id FROM manifesto_sections)")
    if not rows:
        conn.commit()
        return 0
    log.info(f"Embedding {len(rows)} manifesto sections …")
    texts = [f"passage: {r['section_path'] or ''}\n{r['text']}"[:MAX_CHARS] for r in rows]
    vecs = _embed(texts)
    conn.executemany("INSERT OR REPLACE INTO section_embeddings (section_id, vector) VALUES (?, ?)",
                     [(r["id"], v.tobytes()) for r, v in zip(rows, vecs)])
    conn.commit()
    _party_cache.clear()
    return len(rows)


def _party_matrix(conn, party: str) -> tuple[list[int], np.ndarray]:
    if party not in _party_cache:
        rows = conn.execute("""
            SELECT s.id, e.vector FROM manifesto_sections s
            JOIN section_embeddings e ON e.section_id = s.id
            WHERE s.party = ? ORDER BY s.id
        """, (party,)).fetchall()
        ids = [r["id"] for r in rows]
        mat = (np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
               if rows else np.zeros((0, 384), dtype=np.float32))
        _party_cache[party] = (ids, mat)
    return _party_cache[party]


def query_vector(text: str) -> np.ndarray:
    return _embed([f"query: {text}"[:MAX_CHARS]])[0]


def top_sections(conn, party: str, qvec: np.ndarray, top_k: int = 3) -> list[tuple[int, float]]:
    """[(section_id, cosine similarity), …] for the party's closest sections."""
    ids, mat = _party_matrix(conn, party)
    if not ids:
        return []
    sims = mat @ qvec
    best = np.argsort(-sims)[:top_k]
    return [(ids[i], float(sims[i])) for i in best]


# ── Topic assignment for manifesto sections ──────────────────────────────────
# The parliament's 18 topics, each described by a few short German phrases.
# A section belongs to the topic it is clearly closest to; sections that are
# about equally close to every topic (introductions, interviews, chapter
# teasers) get no topic at all.

TOPIC_DESCRIPTIONS: dict[str, list[str]] = {
    "Inneres und Recht": [
        "Polizei, Kriminalität, innere Sicherheit, Terrorismusbekämpfung",
        "Asyl, Migration, Zuwanderung, Abschiebung, Integration, Staatsbürgerschaft",
        "Justiz, Strafrecht, Gerichte, Strafvollzug, Zivilrecht"],
    "Budget und Finanzen": [
        "Staatshaushalt, Budgetdefizit, Staatsschulden, Budgetsanierung",
        "Steuern, Steuerreform, Abgaben, Steuersenkung, Vermögensteuer, Erbschaftssteuer"],
    "Wirtschaft": [
        "Wirtschaftsstandort, Unternehmen, Industrie, Gewerbe, Mittelstand",
        "Wettbewerb, Bürokratieabbau, Gründer, Handel, Export, Konjunktur, Inflation",
        "Tourismus, Gastronomie, Kaufkraft, Teuerung, Preise"],
    "Soziales": [
        "Pensionen, Pflege, Sozialhilfe, Mindestsicherung, Armutsbekämpfung",
        "Menschen mit Behinderung, Inklusion, Sozialversicherung, Sozialstaat",
        "leistbares Wohnen, Mieten, Wohnbau, Gemeindewohnungen, Wohnbauförderung"],
    "Arbeit": [
        "Arbeitsmarkt, Arbeitslosigkeit, Arbeitslosengeld, AMS, Beschäftigung",
        "Löhne, Arbeitszeit, Arbeitnehmerrechte, Fachkräfte, Lehrlinge, Kollektivvertrag"],
    "Gesundheit und Ernährung": [
        "Gesundheitssystem, Spitäler, Ärzte, Krankenkassen, Medikamente",
        "Prävention, psychische Gesundheit, Pandemie, Lebensmittelsicherheit, Ernährung"],
    "Klima, Umwelt und Energie": [
        "Klimaschutz, CO2-Emissionen, Klimaneutralität, Klimakrise",
        "erneuerbare Energie, Strom, Netzausbau, Energiepreise, Gas",
        "Umweltschutz, Naturschutz, Bodenschutz, Artenvielfalt, Kreislaufwirtschaft"],
    "Verkehr und Infrastruktur": [
        "Verkehr, Bahn, öffentlicher Verkehr, Straßenbau, Autobahn, Klimaticket",
        "Infrastruktur, Breitband, Post, Mobilität, Radwege, Transitverkehr"],
    "Information und Medien": [
        "Medien, ORF, Pressefreiheit, Medienförderung, Journalismus",
        "Informationsfreiheit, Desinformation, soziale Medien, Plattformen"],
    "Parlament und Demokratie": [
        "Parlament, Wahlrecht, direkte Demokratie, Volksabstimmung, Verfassung",
        "Korruptionsbekämpfung, Transparenz, Parteienfinanzierung, Untersuchungsausschuss, Föderalismus"],
    "Familie und Generationen": [
        "Familien, Kinder, Kinderbetreuung, Familienbeihilfe, Kinderarmut",
        "Jugend, Eltern, Karenz, ältere Generation, Senioren"],
    "Europäische Union": [
        "Europäische Union, EU-Recht, EU-Kommission, Europäisches Parlament",
        "EU-Erweiterung, Binnenmarkt, Subsidiarität, europäische Integration"],
    "Bildung": [
        "Schule, Lehrer, Unterricht, Kindergarten als Bildungseinrichtung",
        "Universitäten, Hochschulen, Studium, Erwachsenenbildung, Lehrpläne"],
    "Außenpolitik": [
        "Außenpolitik, Neutralität, Diplomatie, internationale Beziehungen",
        "Landesverteidigung, Bundesheer, Krieg in der Ukraine, Nahost, "
        "Entwicklungszusammenarbeit, Menschenrechte weltweit"],
    "Land- und Forstwirtschaft": [
        "Landwirtschaft, Bauern, Agrarförderung, Lebensmittelproduktion",
        "Forstwirtschaft, Wald, Tierhaltung, Tierschutz, ländlicher Raum"],
    "Kultur": [
        "Kunst und Kultur, Kulturförderung, Künstler, Museen, Theater",
        "Sport, Denkmalschutz, kulturelles Erbe, Religion"],
    "Innovation, Technologie und Forschung": [
        "Forschung, Wissenschaft, Innovation, Technologie",
        "Digitalisierung, künstliche Intelligenz, Datenschutz, Cybersicherheit, E-Government"],
    "Frauen und Gleichbehandlung": [
        "Frauen, Gleichstellung, Gender Pay Gap, Gewalt gegen Frauen",
        "Gleichbehandlung, Diskriminierung, LGBTIQ, Antidiskriminierung"],
}

MIN_TOPIC_SCORE  = 0.65   # below this a section is too generic for any topic
SECOND_TOPIC_GAP = 0.15   # a second topic must be this close to the first …
SECOND_TOPIC_MIN = 1.0    # … and clearly topical itself


def assign_topics(conn) -> dict[str, int]:
    """
    Recompute section_topics for all manifesto sections.

    Similarities are standardised per topic (so broad topics don't attract
    everything) and then compared within each section: the score is how far
    a topic stands out from the section's median topic.
    Returns {topic: number of sections}.
    """
    rows = conn.execute("""
        SELECT s.id, e.vector FROM manifesto_sections s
        JOIN section_embeddings e ON e.section_id = s.id ORDER BY s.id
    """).fetchall()
    conn.execute("DELETE FROM section_topics")
    if not rows:
        conn.commit()
        return {}
    topics = list(TOPIC_DESCRIPTIONS)
    tvecs = []
    for t in topics:
        v = _embed([f"query: {t}: {d}" for d in TOPIC_DESCRIPTIONS[t]]).mean(0)
        tvecs.append(v / np.linalg.norm(v))
    sections = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    raw = sections @ np.array(tvecs).T
    z = (raw - raw.mean(0)) / raw.std(0)
    score = z - np.median(z, axis=1, keepdims=True)

    out = []
    for i, r in enumerate(rows):
        first, second = np.argsort(-score[i])[:2]
        if score[i, first] < MIN_TOPIC_SCORE:
            continue
        out.append((r["id"], topics[first], float(score[i, first])))
        if (score[i, first] - score[i, second] <= SECOND_TOPIC_GAP
                and score[i, second] >= SECOND_TOPIC_MIN):
            out.append((r["id"], topics[second], float(score[i, second])))
    conn.executemany("INSERT INTO section_topics (section_id, topic, score) VALUES (?, ?, ?)", out)
    conn.commit()
    counts: dict[str, int] = {}
    for _, t, _ in out:
        counts[t] = counts.get(t, 0) + 1
    log.info(f"Topic assignment: {len(out)} links for {len(rows)} sections, "
             f"{len(rows) - len({o[0] for o in out})} sections without topic")
    return counts


if __name__ == "__main__":
    # python embeddings.py  – embed new sections and recompute topic assignments
    import database as db
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    c = db.connect()
    db.init_schema(c)
    ensure_section_embeddings(c)
    for topic, n in sorted(assign_topics(c).items(), key=lambda x: -x[1]):
        print(f"  {topic:<40} {n:>4}")
