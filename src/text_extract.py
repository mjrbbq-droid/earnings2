# src/text_extract.py
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import pdfplumber


def sha256_file(filepath: str | Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def extract_with_pdfplumber(filepath: str | Path) -> str:
    texts = []
    with pdfplumber.open(str(filepath)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t:
                texts.append(t)
    return "\n".join(texts).strip()


def extract_with_tika(filepath: str | Path) -> str:
    # Optional fallback. Only import if needed.
    from tika import parser  # type: ignore

    parsed = parser.from_file(str(filepath), service="text")
    content = parsed.get("content") or ""
    return content.strip()


def extract_text(filepath: str | Path, min_chars: int = 500) -> str:
    """
    pdfplumber-first. If output is too short/empty, fall back to Tika.
    """
    text = extract_with_pdfplumber(filepath)
    if len(text) >= min_chars:
        return text

    # Fallback
    try:
        text2 = extract_with_tika(filepath)
        if len(text2) >= len(text):
            return text2
        return text
    except Exception:
        return text  # last resort


#     python -m scripts.build_chunks