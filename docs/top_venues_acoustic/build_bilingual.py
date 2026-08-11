#!/usr/bin/env python3
"""Build page-aligned English/Chinese PDFs from the downloaded papers.

Each English page from the publisher PDF is followed by one or more Chinese
translation pages.  Translation responses are cached, so interrupted runs can
be resumed without repeating completed network work.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
ORIGINALS = ROOT / "originals"
OUTPUTS = ROOT / "bilingual_zh_en"
CACHE = ROOT / ".translation_cache"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
MAX_QUERY_CHARS = 2800
MAX_ZH_PAGE_CHARS = 1900
WORKERS = 6


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
    )
    return result.stdout if capture else ""


def page_count(pdf: Path) -> int:
    info = run("pdfinfo", str(pdf), capture=True)
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Could not determine page count: {pdf}")
    return int(match.group(1))


def extract_page(pdf: Path, page: int) -> str:
    text = run(
        "pdftotext",
        "-enc",
        "UTF-8",
        "-f",
        str(page),
        "-l",
        str(page),
        "-nopgbrk",
        str(pdf),
        "-",
        capture=True,
    )
    text = text.replace("\x00", "").replace("\u00ad", "")
    text = re.sub(r"([A-Za-z])-\n([a-z])", r"\1\2", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        candidates = [
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind("; "),
            window.rfind(", "),
            window.rfind(" "),
        ]
        cut = max(candidates)
        if cut < limit // 2:
            cut = limit
        elif window[cut : cut + 2] in {". ", "; ", ", "}:
            cut += 1
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


def translate_chunk(text: str) -> str:
    error: Exception | None = None
    for attempt in range(8):
        try:
            response = requests.get(
                TRANSLATE_URL,
                params={
                    "client": "gtx",
                    "sl": "en",
                    "tl": "zh-CN",
                    "dt": "t",
                    "q": text,
                },
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            translated = "".join(item[0] for item in data[0] if item and item[0])
            if translated.strip():
                return translated.strip()
            raise RuntimeError("Translation service returned empty text")
        except Exception as exc:  # retry transient throttling/network failures
            error = exc
            time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"Translation failed after retries: {error}")


def cache_path(pdf: Path, page: int, text: str) -> Path:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return CACHE / pdf.stem / f"page_{page:03d}_{digest}.json"


def translate_page(pdf: Path, page: int, text: str) -> str:
    target = cache_path(pdf, page, text)
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8"))["zh"]
    if not text.strip():
        translated = "【本页未提取到可翻译文字；请查看前一页英文原版中的图表或公式。】"
    else:
        chunks = split_text(text, MAX_QUERY_CHARS)
        translated = "\n\n".join(translate_chunk(chunk) for chunk in chunks)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"source": text, "zh": translated}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return translated


def split_translation(text: str) -> list[str]:
    return split_text(text, MAX_ZH_PAGE_CHARS)


def make_translation_html(
    pdf: Path, translations: dict[int, str], target: Path
) -> list[int]:
    sections: list[str] = []
    segment_counts: list[int] = []
    total_pages = len(translations)
    for source_page in range(1, total_pages + 1):
        segments = split_translation(translations[source_page])
        segment_counts.append(len(segments))
        for part, segment in enumerate(segments, 1):
            suffix = f" · 译文 {part}/{len(segments)}" if len(segments) > 1 else ""
            sections.append(
                f"""
<section class="page">
  <header>
    <div class="paper">{html.escape(pdf.stem)}</div>
    <div class="page-no">英文原版第 {source_page}/{total_pages} 页{suffix}</div>
  </header>
  <main>{html.escape(segment)}</main>
  <footer>机器辅助全文翻译 · 公式、图表及版式请以前一页英文原版为准</footer>
</section>"""
            )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 12mm 13mm 11mm; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; color: #172033; }}
  body {{ font-family: "Noto Serif CJK SC", "Noto Sans CJK SC", sans-serif; }}
  .page {{ height: 272mm; break-after: page; position: relative; overflow: hidden; }}
  .page:last-child {{ break-after: auto; }}
  header {{ border-bottom: 1.2px solid #5b6b84; padding-bottom: 3mm; margin-bottom: 5mm; }}
  .paper {{ font: 600 9pt/1.35 "Noto Sans CJK SC", sans-serif; color: #44526b; overflow-wrap: anywhere; }}
  .page-no {{ margin-top: 1.5mm; font: 700 13pt/1.35 "Noto Sans CJK SC", sans-serif; color: #102a56; }}
  main {{ white-space: pre-wrap; overflow-wrap: anywhere; font-size: 10.2pt; line-height: 1.62; column-count: 2; column-gap: 9mm; column-rule: 0.4px solid #d8dee9; height: 241mm; }}
  footer {{ position: absolute; bottom: 0; left: 0; right: 0; border-top: 0.5px solid #c7cfdb; padding-top: 1.5mm; text-align: center; font: 7.5pt/1.2 "Noto Sans CJK SC", sans-serif; color: #68768c; }}
</style>
</head>
<body>{''.join(sections)}</body>
</html>"""
    target.write_text(document, encoding="utf-8")
    return segment_counts


def build_one(pdf: Path) -> None:
    output = OUTPUTS / f"{pdf.stem}_中英文对照版.pdf"
    pages = page_count(pdf)
    if output.exists() and page_count(output) >= pages * 2:
        print(f"SKIP {pdf.name} -> existing bilingual PDF", flush=True)
        return

    print(f"TRANSLATE {pdf.name} ({pages} pages)", flush=True)
    source = {page: extract_page(pdf, page) for page in range(1, pages + 1)}
    translations: dict[int, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        future_pages = {
            pool.submit(translate_page, pdf, page, text): page
            for page, text in source.items()
        }
        for future in concurrent.futures.as_completed(future_pages):
            page = future_pages[future]
            translations[page] = future.result()
            print(f"  translated page {page}/{pages}", flush=True)

    with tempfile.TemporaryDirectory(prefix="bilingual_") as tmp_name:
        tmp = Path(tmp_name)
        html_path = tmp / "translation.html"
        counts = make_translation_html(pdf, translations, html_path)
        translated_pdf = tmp / "translation.pdf"
        run(
            "google-chrome",
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={translated_pdf}",
            html_path.as_uri(),
        )
        expected_translation_pages = sum(counts)
        actual_translation_pages = page_count(translated_pdf)
        if actual_translation_pages != expected_translation_pages:
            raise RuntimeError(
                f"Translation pagination mismatch for {pdf.name}: "
                f"expected {expected_translation_pages}, got {actual_translation_pages}"
            )

        run("pdfseparate", str(pdf), str(tmp / "en-%03d.pdf"))
        run("pdfseparate", str(translated_pdf), str(tmp / "zh-%03d.pdf"))
        merge_order: list[str] = []
        zh_index = 1
        for source_page, count in enumerate(counts, 1):
            merge_order.append(str(tmp / f"en-{source_page:03d}.pdf"))
            for _ in range(count):
                merge_order.append(str(tmp / f"zh-{zh_index:03d}.pdf"))
                zh_index += 1
        staged = tmp / "bilingual.pdf"
        run("pdfunite", *merge_order, str(staged))
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, output)
    print(f"DONE {output.name} ({page_count(output)} pages)", flush=True)


def main() -> int:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(ORIGINALS.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {ORIGINALS}", file=sys.stderr)
        return 1
    for pdf in pdfs:
        build_one(pdf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
