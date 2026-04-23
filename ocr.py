"""
PDF -> text using Tesseract. Only used when no AI provider is configured.

On macOS you need:
    brew install tesseract poppler
    pip install pytesseract pdf2image pypdf
"""

from __future__ import annotations

import os
from typing import List


def _check_deps() -> None:
    missing = []
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        missing.append("pytesseract")
    try:
        from pdf2image import convert_from_path  # noqa: F401
    except ImportError:
        missing.append("pdf2image")
    try:
        import pypdf  # noqa: F401
    except ImportError:
        missing.append("pypdf")
    if missing:
        raise ImportError(
            "Missing Python packages: " + ", ".join(missing) +
            "\nInstall with:  pip install " + " ".join(missing)
        )


def _embedded_text(pdf_path: str) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return ""
    chunks: List[str] = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks).strip()


def _ocr_pages(pdf_path: str, dpi: int = 300, max_pages: int | None = None) -> str:
    from pdf2image import convert_from_path
    import pytesseract

    kwargs = {"dpi": dpi}
    if max_pages:
        kwargs["last_page"] = max_pages
    images = convert_from_path(pdf_path, **kwargs)
    chunks = [pytesseract.image_to_string(img) for img in images]
    return "\n".join(chunks).strip()


def extract_text(pdf_path: str, *, force_ocr: bool = False,
                 dpi: int = 300, max_pages: int | None = 3) -> str:
    _check_deps()
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(pdf_path)
    if not force_ocr:
        text = _embedded_text(pdf_path)
        if len(text) >= 50:
            return text
    return _ocr_pages(pdf_path, dpi=dpi, max_pages=max_pages)
