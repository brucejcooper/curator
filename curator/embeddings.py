"""The embedding model (fastembed), loaded on first use: one vector per text,
normalised, so a dot product is the cosine similarity."""

import os
from functools import lru_cache

import numpy as np

EMBED_MODEL = os.environ.get("CURATOR_EMBED_MODEL", "BAAI/bge-small-en-v1.5")


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL)


def embed(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    vecs = np.array(list(_model().embed(texts)), dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
