"""Demo mode: runs the full pipeline flow with no API keys and no cost.

The planner and QA reviewer return canned results, and "generated" images are
local composites of the uploaded photo on styled backgrounds. Useful for trying
the UI, recording a walkthrough, or running the app before keys are set up.
"""

from __future__ import annotations

import asyncio
import io
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

DEMO_PLAN = {
    "product": {
        "name": "Your product",
        "category": "Demo run (add API keys for real analysis)",
        "key_features": ["Detected from the photo", "Specific to your product", "Used in the ad copy"],
        "audience": "your target customers",
    },
    "concepts": [
        {
            "title": "Night setup",
            "angle": "Aspirational: shows the product as the centerpiece of a dream setup.",
            "platform": "Instagram feed",
            "edit_prompt": "Place this exact product on a dark desk with soft purple and blue RGB glow ...",
            "headline": "Level up your setup",
            "caption": "Built for the late-night sessions. Precision you can feel, style you can see. Tap to shop before it sells out.",
            "hashtags": ["setupgoals", "gaminggear", "desksetup"],
        },
        {
            "title": "Clean studio hero",
            "angle": "Premium: lets the product's design speak with zero distractions.",
            "platform": "Facebook",
            "edit_prompt": "Place this exact product on a seamless light studio backdrop with a soft shadow ...",
            "headline": "Design that performs",
            "caption": "Every detail engineered for comfort and control. See why players are switching. Shop now with fast delivery.",
            "hashtags": ["minimalsetup", "techdesign", "newarrival"],
        },
        {
            "title": "Golden hour desk",
            "angle": "Lifestyle: shows the product in a warm, real-life moment.",
            "platform": "TikTok",
            "edit_prompt": "Place this exact product on a wooden desk by a window at golden hour ...",
            "headline": "Your everyday upgrade",
            "caption": "From work to play, it just fits. Comfortable all day, ready when the game starts. Grab yours today.",
            "hashtags": ["dailysetup", "workfromhome", "gamingsetup"],
        },
    ],
}

PALETTES = [
    ((18, 14, 38), (88, 52, 190)),
    ((236, 236, 240), (200, 202, 212)),
    ((92, 56, 30), (232, 168, 92)),
]


def _composite(photo: bytes, style: int) -> bytes:
    product = Image.open(io.BytesIO(photo)).convert("RGB")
    w, h = product.size
    top, bottom = PALETTES[style]
    bg = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(bg)
    for y in range(h):
        t = y / max(h - 1, 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))

    scale = 0.62
    thumb = product.resize((int(w * scale), int(h * scale)))
    x, y = (w - thumb.width) // 2, int(h * 0.16)

    shadow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [x + 10, y + 24, x + thumb.width + 10, y + thumb.height + 24], radius=28, fill=150
    )
    bg.paste((0, 0, 0), mask=shadow.filter(ImageFilter.GaussianBlur(28)))

    mask = Image.new("L", thumb.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, thumb.width, thumb.height], radius=24, fill=255)
    bg.paste(thumb, (x, y), mask)

    out = io.BytesIO()
    bg.save(out, "JPEG", quality=88)
    return out.getvalue()


async def demo_pipeline(photo: bytes, notes: str, run_dir: Path, emit) -> None:
    await emit({"type": "step", "step": "analyze", "status": "running"})
    await asyncio.sleep(1.2)
    await emit({"type": "step", "step": "analyze", "status": "done"})
    await emit({"type": "plan", **DEMO_PLAN})
    await emit({"type": "step", "step": "generate", "status": "running"})

    async def one(i: int) -> None:
        await emit({"type": "concept", "index": i, "status": "generating", "attempt": 1})
        await asyncio.sleep(1.0 + random.random() * 1.5)
        await emit({"type": "concept", "index": i, "status": "reviewing", "attempt": 1})
        await asyncio.sleep(0.8)
        attempt = 1
        if i == 1:  # show the self-correction loop once
            await emit({"type": "concept", "index": i, "status": "retrying",
                        "qa": {"score": 6, "issues": "Logo slightly blurred", "fix": "Keep the logo sharp"}})
            attempt = 2
            await emit({"type": "concept", "index": i, "status": "generating", "attempt": 2})
            await asyncio.sleep(1.4)
            await emit({"type": "concept", "index": i, "status": "reviewing", "attempt": 2})
            await asyncio.sleep(0.6)
        path = run_dir / f"concept{i + 1}_demo.jpg"
        path.write_bytes(_composite(photo, i))
        await emit({
            "type": "concept", "index": i, "status": "done", "attempt": attempt,
            "url": f"/outputs/{run_dir.name}/{path.name}",
            "qa": {"score": [9, 8, 9][i], "product_preserved": True, "issues": "", "fix": ""},
        })

    await asyncio.gather(*(one(i) for i in range(3)))
    await emit({"type": "step", "step": "generate", "status": "done"})
