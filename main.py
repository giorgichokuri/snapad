"""SnapAd web server. Run with:  uvicorn app.main:app --reload"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import pipeline  # noqa: E402

log = logging.getLogger("snapad")
MAX_UPLOAD = 10 * 1024 * 1024
ACCESS_CODE = os.getenv("SNAPAD_ACCESS_CODE", "")  # protects your API credits on a public deploy

app = FastAPI(title="SnapAd", description="Product photo to ready-to-post ad creatives")
pipeline.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=pipeline.OUTPUT_DIR), name="outputs")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(pipeline.ROOT / "static" / "index.html")


@app.get("/api/config")
async def config() -> dict:
    return {
        "demo": pipeline.demo_mode(),
        "llm_model": pipeline.LLM_MODEL,
        "image_model": pipeline.IMAGE_MODEL,
        "qa_threshold": pipeline.QA_THRESHOLD,
        "access_code_required": bool(ACCESS_CODE) and not pipeline.demo_mode(),
    }


@app.post("/api/generate")
async def generate(image: UploadFile = File(...), notes: str = Form(""), code: str = Form("")) -> StreamingResponse:
    if ACCESS_CODE and not pipeline.demo_mode() and not secrets.compare_digest(code, ACCESS_CODE):
        raise HTTPException(401, "Wrong access code.")
    if not (image.content_type or "").startswith("image/"):
        raise HTTPException(400, "Please upload an image file (JPG, PNG or WebP).")
    raw = await image.read()
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "Image is larger than 10 MB.")

    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            await pipeline.run_pipeline(raw, notes[:500], emit)
        except Exception as exc:  # surface any failure to the UI instead of hanging
            log.exception("pipeline failed")
            await emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())

    async def stream():
        try:
            while (event := await queue.get()) is not None:
                yield json.dumps(event) + "\n"
        finally:
            task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
