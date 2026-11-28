"""调用本机 Ollama，把一批文本转成与输入顺序对应的向量。"""

import logging
from json import dumps
from math import isfinite
from time import monotonic

import httpx
from pydantic import BaseModel, ValidationError

from jobpilot.config import EMBEDDING_DIMENSIONS, settings

logger = logging.getLogger("uvicorn.error").getChild("jobpilot")


class EmbeddingUnavailable(RuntimeError):
    """Ollama 无法返回完整、可用的向量。"""


class _EmbedResponse(BaseModel):
    embeddings: list[list[float]]


def _valid_vector(vector: list[float]) -> bool:
    return (
        len(vector) == EMBEDDING_DIMENSIONS
        and all(isfinite(value) for value in vector)
        and any(vector)
    )


async def embed_texts(
    texts: list[str], model: str, *, timeout: float
) -> list[list[float]]:
    """拒绝代理、重定向、截断输入和不适合余弦距离的向量。"""
    if not texts:
        return []
    started = monotonic()
    succeeded = False
    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url, timeout=timeout, trust_env=False
        ) as client:
            response = await client.post(
                "/api/embed", json={"model": model, "input": texts, "truncate": False}
            )
            response.raise_for_status()
        vectors = _EmbedResponse.model_validate_json(response.content).embeddings
        if len(vectors) != len(texts) or not all(map(_valid_vector, vectors)):
            raise EmbeddingUnavailable("本机 Ollama 返回的向量规格不符")
        succeeded = True
        return vectors
    except (httpx.HTTPError, ValidationError):
        raise EmbeddingUnavailable("本机 Ollama 暂不可用") from None
    finally:
        metrics = {
            "event": "ollama_embedding",
            "model": model,
            "input_count": len(texts),
            "duration_ms": round((monotonic() - started) * 1000),
            "success": succeeded,
        }
        logger.log(
            logging.INFO if succeeded else logging.WARNING,
            "%s",
            dumps(metrics, ensure_ascii=False),
        )
