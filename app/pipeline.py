"""SnapAd pipeline: product photo -> 3 ready-to-post ad creatives.

Steps
  1. analyze   Claude (vision) identifies the product and plans 3 ad concepts + copy.
  2. generate  FLUX Kontext (Replicate) re-shoots the product in each concept's scene.
  3. review    Claude (vision) compares each result with the original photo.
               Images that fail QA are regenerated once with the reviewer's fix.

Every step reports progress through `emit(event)` so the UI can stream it live.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image, ImageOps

from . import prompts

Emit = Callable[[dict[str, Any]], Awaitable[None]]

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "outputs"

LLM_MODEL = os.getenv("SNAPAD_LLM_MODEL", "claude-sonnet-5-5")
IMAGE_MODEL = os.getenv("SNAPAD_IMAGE_MODEL", "black-forest-labs/flux-kontext-pro")
QA_THRESHOLD = int(os.getenv("SNAPAD_QA_THRESHOLD", "7"))
MAX_ATTEMPTS = 2  # first try + one QA-driven retry


def demo_mode() -> bool:
    if os.getenv("SNAPAD_DEMO", "").strip() in {"1", "true", "yes"}:
        return True
    return not (os.getenv("ANTHROPIC_API_KEY") and os.getenv("REPLICATE_API_TOKEN"))


# ---------------------------------------------------------------- helpers

def prepare_image(raw: bytes, max_side: int = 1024) -> bytes:
    """Normalise an upload: fix rotation, convert to RGB, cap the size, re-encode as JPEG."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def image_block(data: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(data)}}


def parse_json(text: str) -> dict[str, Any]:
    """LLMs sometimes wrap JSON in fences or add a sentence; take the outermost object."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model did not return JSON")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- model calls

async def ask_claude(system: str, content: list[dict[str, Any]], max_tokens: int = 2000) -> dict[str, Any]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    msg = await client.messages.create(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(block.text for block in msg.content if block.type == "text")
    return parse_json(text)


async def analyze(photo: bytes, notes: str) -> dict[str, Any]:
    notes_block = f"Seller notes: {notes.strip()}\n" if notes.strip() else ""
    plan = await ask_claude(
        prompts.CREATIVE_DIRECTOR_SYSTEM,
        [image_block(photo), {"type": "text", "text": prompts.CREATIVE_DIRECTOR_USER.format(notes_block=notes_block)}],
    )
    concepts = plan.get("concepts") or []
    if len(concepts) < 3:
        raise ValueError("Planner returned fewer than 3 concepts")
    plan["concepts"] = concepts[:3]
    return plan


async def generate_image(photo: bytes, instruction: str) -> bytes:
    import replicate

    def _run() -> Any:
        return replicate.run(
            IMAGE_MODEL,
            input={
                "prompt": instruction,
                "input_image": f"data:image/jpeg;base64,{b64(photo)}",
                "aspect_ratio": "match_input_image",
                "output_format": "jpg",
            },
        )

    output = await asyncio.to_thread(_run)
    if isinstance(output, list):
        output = output[0]
    if hasattr(output, "read"):
        return await asyncio.to_thread(output.read)
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.get(str(output))
        resp.raise_for_status()
        return resp.content


async def review(photo: bytes, generated: bytes, title: str) -> dict[str, Any]:
    result = await ask_claude(
        prompts.QA_SYSTEM,
        [
            image_block(photo),
            image_block(prepare_image(generated, 768)),
            {"type": "text", "text": prompts.QA_USER.format(title=title)},
        ],
        max_tokens=400,
    )
    result["score"] = int(result.get("score", 0))
    return result


# ---------------------------------------------------------------- orchestration

async def produce_concept(i: int, concept: dict, photo: bytes, run_dir: Path, emit: Emit) -> dict:
    instruction = concept["edit_prompt"]
    best: dict[str, Any] | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        await emit({"type": "concept", "index": i, "status": "generating", "attempt": attempt})
        image = await generate_image(photo, instruction)

        await emit({"type": "concept", "index": i, "status": "reviewing", "attempt": attempt})
        qa = await review(photo, image, concept["title"])

        path = run_dir / f"concept{i + 1}_try{attempt}.jpg"
        path.write_bytes(image)
        candidate = {"url": f"/outputs/{run_dir.name}/{path.name}", "qa": qa, "attempt": attempt}
        if best is None or qa["score"] > best["qa"]["score"]:
            best = candidate

        if qa["score"] >= QA_THRESHOLD or attempt == MAX_ATTEMPTS:
            break
        await emit({"type": "concept", "index": i, "status": "retrying", "qa": qa})
        instruction = f"{concept['edit_prompt']} Important correction: {qa.get('fix') or qa.get('issues')}"

    assert best is not None
    await emit({"type": "concept", "index": i, "status": "done", **best})
    return best


async def run_pipeline(raw_photo: bytes, notes: str, emit: Emit) -> None:
    started = time.monotonic()
    photo = prepare_image(raw_photo)
    run_dir = OUTPUT_DIR / uuid.uuid4().hex[:12]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "original.jpg").write_bytes(photo)

    if demo_mode():
        from .demo import demo_pipeline

        await demo_pipeline(photo, notes, run_dir, emit)
    else:
        await emit({"type": "step", "step": "analyze", "status": "running"})
        plan = await analyze(photo, notes)
        await emit({"type": "step", "step": "analyze", "status": "done"})
        await emit({"type": "plan", "product": plan["product"], "concepts": plan["concepts"]})

        await emit({"type": "step", "step": "generate", "status": "running"})
        results = await asyncio.gather(
            *(produce_concept(i, c, photo, run_dir, emit) for i, c in enumerate(plan["concepts"]))
        )
        await emit({"type": "step", "step": "generate", "status": "done"})
        (run_dir / "result.json").write_text(json.dumps({"plan": plan, "results": results}, indent=2))

    await emit({"type": "done", "seconds": round(time.monotonic() - started, 1)})
