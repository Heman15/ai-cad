# SPDX-License-Identifier: AGPL-3.0-or-later
"""Uploaded source drawings (PDF or image): content-addressed storage and page rendering.

Files are stored outside the repository under ~/.cache/ai-cad/drawings/<sha256>/ (or $AI_CAD_CACHE).
The project file keeps only the hash and file name, so drawings are traceable without bloating
the project; if the cache is cleared the analysis remains but page images are reported missing.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

MAX_PAGES = 4
MAX_SIDE_PX = 1800
TYPES = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def cache_dir() -> Path:
    return Path(os.environ.get("AI_CAD_CACHE", Path.home() / ".cache" / "ai-cad")) / "drawings"


def store(data: bytes, filename: str, media_type: str) -> dict:
    if media_type not in TYPES:
        raise ValueError(f"unsupported drawing type {media_type}; use PDF, PNG, JPEG or WebP")
    sha = hashlib.sha256(data).hexdigest()
    d = cache_dir() / sha
    d.mkdir(parents=True, exist_ok=True)
    (d / f"original{TYPES[media_type]}").write_bytes(data)
    pages = _render_pages(data, media_type)
    for i, png in enumerate(pages, 1):
        (d / f"page-{i}.png").write_bytes(png)
    return {"sha256": sha, "filename": filename, "media_type": media_type, "pages": len(pages)}


def page_png(sha: str, page: int) -> bytes | None:
    if not sha.isalnum():
        return None
    p = cache_dir() / sha / f"page-{int(page)}.png"
    return p.read_bytes() if p.is_file() else None


def _render_pages(data: bytes, media_type: str) -> list[bytes]:
    from PIL import Image

    images: list[Image.Image] = []
    if media_type == "application/pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        for i in range(min(len(pdf), MAX_PAGES)):
            page = pdf[i]
            w, h = page.get_size()  # points
            scale = MAX_SIDE_PX / max(w, h)
            images.append(page.render(scale=scale).to_pil())
    else:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
        images.append(img)
    out = []
    for img in images:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        out.append(buf.getvalue())
    return out
