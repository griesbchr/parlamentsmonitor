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
