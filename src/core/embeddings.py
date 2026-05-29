"""Локальные эмбеддинги через BAAI/bge-m3 (мультиязычный, 1024 dim)."""

import asyncio
from threading import Lock

import structlog

from src.config import settings

log = structlog.get_logger()

_model = None
_lock = Lock()


def _load_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                log.info("embeddings.loading", model=settings.embedding_model)
                _model = SentenceTransformer(settings.embedding_model, device="cpu")
                log.info("embeddings.loaded")
    return _model


async def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = _load_model()

    def _run() -> list[list[float]]:
        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    return await asyncio.to_thread(_run)


async def embed_one(text: str) -> list[float]:
    if not text or not text.strip():
        return [0.0] * settings.embedding_dim
    vecs = await embed([text])
    return vecs[0]


async def warmup() -> None:
    """Прогрев модели в фоне на старте, чтобы первый запрос не подвисал."""
    await embed_one("warmup")
