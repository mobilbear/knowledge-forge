"""Capture original documents and extract page-addressable text locally."""
from __future__ import annotations

import re
import subprocess


MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 300


def extract_pdf(data: bytes, title: str, platform: str = "manual") -> dict:
    if not data.startswith(b"%PDF-") or len(data) > MAX_PDF_BYTES:
        raise ValueError("document is not a PDF or exceeds the PDF size limit")
    result = {
        "platform": platform, "title": title, "content_kind": "document",
        "capture_method": "manual" if platform == "manual" else "direct",
        "artifacts": [("original.pdf", data)], "text": "",
    }
    try:
        info = subprocess.run(["pdfinfo", "-"], input=data, capture_output=True,
                              timeout=8, check=True)
        count = re.search(rb"(?m)^Pages:\s+(\d+)", info.stdout)
        if not count or int(count[1]) > MAX_PDF_PAGES:
            result["adapter_error"] = "PDF saved; page count unavailable or above the local limit"
            return result
        if platform == "arxiv":
            metadata_title = re.search(rb"(?m)^Title:[ \t]+([^\n]+)", info.stdout)
            if metadata_title and metadata_title[1].strip():
                result["title"] = metadata_title[1].decode("utf-8", errors="replace").strip()
        text = subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", "-", "-"],
                              input=data, capture_output=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        result["adapter_error"] = "PDF saved; local text extraction failed"
        return result
    pages = text.stdout.decode("utf-8", errors="replace").split("\f")
    # Poppler terminates each page with a form feed. Preserve truly blank pages.
    if pages and not pages[-1].strip():
        pages.pop()
    sections = [(number, page.strip()) for number, page in enumerate(pages, 1) if page.strip()]
    if not sections:
        result["adapter_error"] = "PDF saved; this document needs page OCR"
        return result
    result["text"] = "\n\n".join(f"### Page {number}\n\n{page}" for number, page in sections)
    result["body_verified"] = len(sections) == int(count[1])
    if not result["body_verified"]:
        result["partial_reason"] = "some PDF pages have no extractable text"
    return result
