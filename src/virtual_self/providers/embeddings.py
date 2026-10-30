from __future__ import annotations

import json
import math
import urllib.request
from typing import Sequence

from virtual_self.config import get_settings


def embed_texts(texts: Sequence[str], timeout: float = 120.0) -> list[list[float] | None]:
    settings = get_settings()
    base = settings.ollama_url.rstrip("/")
    model = settings.embed_model
    trimmed = [t[:8000] for t in texts]
    if not trimmed:
        return []
    try:
        req = urllib.request.Request(
            f"{base}/api/embed",
            data=json.dumps({"model": model, "input": trimmed}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        embs = data.get("embeddings") or []
        out: list[list[float] | None] = []
        for vec in embs:
            out.append([float(x) for x in vec] if vec else None)
        while len(out) < len(trimmed):
            out.append(None)
        return out
    except Exception:
        return [None] * len(trimmed)


def embed_one(text: str) -> list[float] | None:
    res = embed_texts([text])
    return res[0] if res else None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"
