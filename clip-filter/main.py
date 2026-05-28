"""CLIP-based image pre-filter for the Aile Scape Game pipeline.

Sits between the image acquisition step and Qwen-VL validate_image. The
filter computes a cheap cosine similarity between an image and a textual
prompt ('<place_name> monument architecture façade'); images below the
threshold (default 0.18) are rejected BEFORE the expensive Qwen-VL call.

Why it matters:
  - Qwen-VL costs ~$0.005 per validate_image call.
  - For 18 candidate images × 1000 places = $90 just on validation.
  - CLIP-ViT-B-32 inference is ~50-100ms on CPU, $0 marginal cost.
  - Filtering 50% before Qwen-VL halves the validation bill.

Endpoint:
  POST /score
    {"image_url": "https://...", "prompt_text": "Porte Saint-Martin monument"}
    → {"similarity": 0.23, "ms": 87, "image_size": [1024, 768]}

  POST /score_batch
    {"prompt_text": "...", "image_urls": ["...", "..."]}
    → {"results": [{"image_url": "...", "similarity": 0.23, "ms": 12}, ...]}

  GET /health
    → {"status": "ok", "model_loaded": true}
"""
import io
import logging
import time
from typing import Optional

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from PIL import Image
from sentence_transformers import SentenceTransformer


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("clip_filter")

app = FastAPI(title="Aile CLIP image filter", version="1.0")

_MODEL_NAME = "sentence-transformers/clip-ViT-B-32"
_model: Optional[SentenceTransformer] = None


@app.on_event("startup")
async def _load_model():
    """Load the CLIP model into memory at boot. ~170 MB, ~3 s on a Mac."""
    global _model
    log.info("Loading CLIP model %s ...", _MODEL_NAME)
    t0 = time.time()
    _model = SentenceTransformer(_MODEL_NAME)
    log.info("CLIP model loaded in %.1fs", time.time() - t0)


class ScoreRequest(BaseModel):
    image_url: str
    prompt_text: str = Field(..., min_length=2, max_length=400)


class ScoreResponse(BaseModel):
    similarity: float
    ms: float
    image_size: Optional[list[int]] = None
    error: Optional[str] = None


class ScoreBatchRequest(BaseModel):
    prompt_text: str = Field(..., min_length=2, max_length=400)
    image_urls: list[str] = Field(..., min_length=1, max_length=30)


class ScoreBatchResponse(BaseModel):
    results: list[dict]
    prompt_ms: float


async def _fetch_image(url: str) -> Image.Image:
    """Download an image and return as PIL.Image (RGB)."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": "AileScapeGame-CLIPFilter/1.0"},
        ) as client:
            r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(400, f"Image fetch HTTP {r.status_code}")
        content_type = (r.headers.get("content-type") or "").lower()
        if not content_type.startswith("image/"):
            raise HTTPException(400, f"Not an image (content-type={content_type})")
        if len(r.content) > 12 * 1024 * 1024:
            raise HTTPException(400, "Image >12MB, skipping")
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Image load failed: {e}") from None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


@app.post("/score", response_model=ScoreResponse)
async def score(req: ScoreRequest):
    """Score a single image against the prompt."""
    if _model is None:
        raise HTTPException(503, "Model not yet loaded")
    t0 = time.time()
    try:
        img = await _fetch_image(req.image_url)
    except HTTPException as e:
        return ScoreResponse(similarity=0.0, ms=(time.time() - t0) * 1000.0, error=str(e.detail))

    text_emb = _model.encode(req.prompt_text, convert_to_numpy=True)
    img_emb = _model.encode(img, convert_to_numpy=True)
    sim = _cosine(text_emb, img_emb)
    return ScoreResponse(
        similarity=round(sim, 4),
        ms=round((time.time() - t0) * 1000.0, 1),
        image_size=list(img.size),
    )


@app.post("/score_batch", response_model=ScoreBatchResponse)
async def score_batch(req: ScoreBatchRequest):
    """Score N images against ONE prompt — encodes the text once, image
    embeddings happen in parallel."""
    if _model is None:
        raise HTTPException(503, "Model not yet loaded")

    t_prompt_start = time.time()
    text_emb = _model.encode(req.prompt_text, convert_to_numpy=True)
    prompt_ms = (time.time() - t_prompt_start) * 1000.0

    import asyncio
    fetches = await asyncio.gather(
        *[_fetch_image(u) for u in req.image_urls],
        return_exceptions=True,
    )

    results: list[dict] = []
    for url, img_or_err in zip(req.image_urls, fetches):
        t_img_start = time.time()
        if isinstance(img_or_err, Exception):
            results.append({
                "image_url": url,
                "similarity": 0.0,
                "ms": round((time.time() - t_img_start) * 1000.0, 1),
                "error": f"fetch_failed: {type(img_or_err).__name__}",
            })
            continue
        try:
            img_emb = _model.encode(img_or_err, convert_to_numpy=True)
            sim = _cosine(text_emb, img_emb)
            results.append({
                "image_url": url,
                "similarity": round(sim, 4),
                "ms": round((time.time() - t_img_start) * 1000.0, 1),
                "image_size": list(img_or_err.size),
            })
        except Exception as e:  # noqa: BLE001
            results.append({
                "image_url": url,
                "similarity": 0.0,
                "ms": round((time.time() - t_img_start) * 1000.0, 1),
                "error": f"encode_failed: {e}",
            })
    return ScoreBatchResponse(results=results, prompt_ms=round(prompt_ms, 1))


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": _model is not None, "model": _MODEL_NAME}
