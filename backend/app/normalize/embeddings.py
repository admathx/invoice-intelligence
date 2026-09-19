"""Local embedding model for description similarity matching (SPEC.md §2: "avoid
an API dep in a hot loop"). Loaded lazily and cached at module scope — loading
the model is the expensive part (~seconds), encoding a single description is fast.
"""
from sentence_transformers import SentenceTransformer

MODEL_NAME = "all-MiniLM-L6-v2"  # 384-dim, matches canonical_skus.description_embedding

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_text(text: str) -> list[float]:
    vector = _get_model().encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = _get_model().encode(texts, normalize_embeddings=True)
    return vectors.tolist()
