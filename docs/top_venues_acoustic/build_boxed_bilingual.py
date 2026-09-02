#!/usr/bin/env python3
"""Rebuild selected papers as paragraph-aligned English/Chinese PDFs.

The output follows the reading layout used by papers 02 and 03: every source
fragment is placed in the left cell of a bordered row, with its Chinese
translation in the right cell.  Page-level extraction and translations are
cached so the build can be resumed safely.
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
import xml.etree.ElementTree as ET
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
ORIGINALS = ROOT / "originals"
OUTPUTS = ROOT / "bilingual_zh_en"
PAGE_CACHE = ROOT / ".translation_cache"
BLOCK_CACHE = ROOT / ".boxed_translation_cache"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
WORKERS = 6
MAX_BLOCK_CHARS = 820
PAPER_NUMBERS = {"05", "06", "07", "08"}

TITLES_ZH = {
    "05": "神经声场学习",
    "06": "用于神经脉冲响应场的声学体渲染",
    "07": "随时随地聆听任何声音",
    "08": "在任何环境中随处聆听",
}


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


def cached_pages(pdf: Path) -> list[dict[str, str]]:
    directory = PAGE_CACHE / pdf.stem
    pages: list[dict[str, str]] = []
    for page in range(1, page_count(pdf) + 1):
        candidates = sorted(directory.glob(f"page_{page:03d}_*.json"))
        if not candidates:
            raise FileNotFoundError(f"Missing page cache: {pdf.name}, page {page}")
        pages.append(json.loads(candidates[-1].read_text(encoding="utf-8")))
    return pages


def clean_block(text: str) -> str:
    text = text.strip()
    text = re.sub(r"(?<=[A-Za-z])-\n(?=[a-z])", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text


def split_at_limit(text: str, limit: int = MAX_BLOCK_CHARS) -> list[str]:
    """Split a long extracted block at natural sentence/line boundaries."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        candidates = [
            window.rfind(". "),
            window.rfind("? "),
            window.rfind("! "),
            window.rfind("; "),
            window.rfind("\n"),
            window.rfind(", "),
            window.rfind(" "),
        ]
        cut = max(candidates)
        if cut < limit // 2:
            cut = limit
        elif window[cut : cut + 2] in {". ", "? ", "! ", "; ", ", "}:
            cut += 1
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


def page_blocks(source: str) -> list[str]:
    blocks: list[str] = []
    for raw in re.split(r"\n\s*\n", source):
        block = clean_block(raw)
        if not block:
            continue
        blocks.extend(split_at_limit(block))
    return blocks


def is_reference_block(text: str, references_started: bool) -> bool:
    if references_started:
        return True
    return bool(re.match(r"^(references|bibliography)\b", text.strip(), re.I))


def is_equation_block(text: str) -> bool:
    compact = " ".join(text.split())
    if len(compact) > 240 or "=" not in compact:
        return False
    words = re.findall(r"[A-Za-z]{3,}", compact)
    return len(words) <= 14 and bool(
        re.search(r"\(\d+\)\s*$", compact)
        or re.search(r"[∑∏∫∂σξζψϕθλ]|[_^{}]", compact)
    )


def block_cache_path(pdf: Path, text: str) -> Path:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return BLOCK_CACHE / pdf.stem / f"{digest}.json"


def translate_block(pdf: Path, text: str, preserve_english: bool) -> str:
    if preserve_english:
        return text
    target = block_cache_path(pdf, text)
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8"))["zh"]
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
            if not translated.strip():
                raise RuntimeError("Translation service returned empty text")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"source": text, "zh": translated.strip()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return translated.strip()
        except Exception as exc:
            error = exc
            time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"Translation failed after retries: {error}")


def paper_title(pdf: Path, pages: list[dict[str, str]]) -> str:
    first = page_blocks(pages[0]["source"])
    if first:
        return " ".join(first[0].splitlines())
    return pdf.stem.removeprefix(pdf.stem[:3]).replace("_", " ")


def extract_inline_figures(pdf: Path, target: Path) -> dict[int, list[tuple[int, Path]]]:
    """Render figure regions and associate them with their original captions."""
    xml_path = target / "layout.xml"
    run("pdftohtml", "-xml", "-hidden", str(pdf), str(xml_path), capture=True)
    tree = ET.parse(xml_path)
    result: dict[int, list[tuple[int, Path]]] = {}
    xml_dpi = 108.0  # pdftohtml's default coordinate scale is 150% of 72 dpi.
    render_dpi = 180.0
    scale = render_dpi / xml_dpi

    for page in tree.getroot().findall("page"):
        page_no = int(page.attrib["number"])
        page_width = int(page.attrib["width"])
        page_height = int(page.attrib["height"])
        texts = []
        captions = []
        for node in page.findall("text"):
            value = "".join(node.itertext()).strip()
            left = int(node.attrib["left"])
            top = int(node.attrib["top"])
            width = int(node.attrib["width"])
            height = int(node.attrib["height"])
            texts.append((left, top, left + width, top + height, value))
            match = re.match(r"^Figure\s*(\d+)\s*[.:]", value, re.I)
            if match:
                captions.append(
                    {
                        "number": int(match.group(1)),
                        "left": left,
                        "top": top,
                        "right": left + width,
                        "images": [],
                    }
                )
        if not captions:
            continue

        images = []
        for node in page.findall("image"):
            left = int(node.attrib["left"])
            top = int(node.attrib["top"])
            right = left + int(node.attrib["width"])
            bottom = top + int(node.attrib["height"])
            x0, x1 = sorted((left, right))
            y0, y1 = sorted((top, bottom))
            if (x1 - x0) * (y1 - y0) >= 150:
                images.append((x0, y0, x1, y1))

        # Assign each embedded raster panel to the nearest following caption.
        # Horizontal distance disambiguates two side-by-side figures.
        for box in images:
            x0, y0, x1, y1 = box
            candidates = [
                cap for cap in captions if -12 <= cap["top"] - y1 <= 420
            ]
            if not candidates:
                continue
            center_x = (x0 + x1) / 2
            nearest = min(
                candidates,
                key=lambda cap: (cap["top"] - y1) + 0.22 * abs(center_x - cap["left"]),
            )
            nearest["images"].append(box)

        page_figures: list[tuple[int, Path]] = []
        content_left = min((item[0] for item in texts), default=45)
        content_right = max((item[2] for item in texts), default=page_width - 45)
        for index, cap in enumerate(captions, 1):
            assigned = cap["images"]
            if assigned:
                x0 = max(content_left, min(box[0] for box in assigned) - 18)
                x1 = min(content_right, max(box[2] for box in assigned) + 18)
                y0 = max(0, min(box[1] for box in assigned) - 28)
                y1 = min(page_height, cap["top"] - 4)
            else:
                # Vector-only charts do not appear as PDF image objects.  Keep
                # their rendered region using the caption position and column.
                nearby = [
                    item
                    for item in texts
                    if cap["top"] <= item[1] <= cap["top"] + 70
                ]
                nearby_right = max((item[2] for item in nearby), default=cap["right"])
                if cap["left"] > page_width * 0.52:
                    x0, x1 = page_width // 2, content_right
                elif nearby_right < page_width * 0.55:
                    x0, x1 = content_left, page_width // 2
                else:
                    x0, x1 = content_left, content_right
                previous_caption = max(
                    (other["top"] for other in captions if other["top"] < cap["top"]),
                    default=0,
                )
                y0 = max(previous_caption + 35, cap["top"] - 270, 0)
                y1 = max(y0 + 30, cap["top"] - 4)

            crop_x = max(0, round(x0 * scale))
            crop_y = max(0, round(y0 * scale))
            crop_w = max(1, round((x1 - x0) * scale))
            crop_h = max(1, round((y1 - y0) * scale))
            prefix = target / f"figure-p{page_no:03d}-{cap['number']:02d}-{index}"
            run(
                "pdftoppm",
                "-f",
                str(page_no),
                "-l",
                str(page_no),
                "-singlefile",
                "-jpeg",
                "-r",
                str(round(render_dpi)),
                "-x",
                str(crop_x),
                "-y",
                str(crop_y),
                "-W",
                str(crop_w),
                "-H",
                str(crop_h),
                str(pdf),
                str(prefix),
            )
            page_figures.append((cap["number"], prefix.with_suffix(".jpg")))
        result[page_no] = page_figures
    return result


def make_html(
    pdf: Path,
    pages: list[dict[str, str]],
    translated: dict[tuple[int, int], str],
    figures: dict[int, list[tuple[int, Path]]],
    target: Path,
) -> None:
    number = pdf.stem[:2]
    title = paper_title(pdf, pages)
    title_zh = TITLES_ZH[number]
    page_sections: list[str] = []
    blocks_by_page = {
        page_no: page_blocks(page["source"])
        for page_no, page in enumerate(pages, 1)
    }
    figure_paths = {
        figure_no: figure_path
        for page_figures in figures.values()
        for figure_no, figure_path in page_figures
    }
    caption_pattern = re.compile(r"(?:^|\n)Figure\s*(\d+)\s*[.:]", re.I)
    reference_pattern = re.compile(r"\b(?:Figure|Fig\.)\s*(\d+)\b", re.I)
    caption_units: dict[int, tuple[str, str]] = {}
    caption_keys: set[tuple[int, int]] = set()
    for page_no, blocks in blocks_by_page.items():
        for block_no, block in enumerate(blocks):
            caption_numbers = [
                int(match.group(1)) for match in caption_pattern.finditer(block)
            ]
            if not caption_numbers:
                continue
            caption_keys.add((page_no, block_no))
            for figure_no in caption_numbers:
                if figure_no in figure_paths:
                    caption_units[figure_no] = (
                        block,
                        translated[(page_no, block_no)],
                    )

    # Place each figure after the first body paragraph that explicitly cites
    # it. If a figure is never cited, retain its original caption position.
    anchors: dict[tuple[int, int], list[int]] = {}
    anchored: set[int] = set()
    for page_no, blocks in blocks_by_page.items():
        for block_no, block in enumerate(blocks):
            key = (page_no, block_no)
            if key in caption_keys:
                continue
            for match in reference_pattern.finditer(block):
                figure_no = int(match.group(1))
                if figure_no in figure_paths and figure_no not in anchored:
                    anchors.setdefault(key, []).append(figure_no)
                    anchored.add(figure_no)
    for page_no, blocks in blocks_by_page.items():
        for block_no, block in enumerate(blocks):
            key = (page_no, block_no)
            if key not in caption_keys:
                continue
            for match in caption_pattern.finditer(block):
                figure_no = int(match.group(1))
                if figure_no in figure_paths and figure_no not in anchored:
                    anchors.setdefault(key, []).append(figure_no)
                    anchored.add(figure_no)

    def figure_unit(figure_no: int) -> str:
        caption_en, caption_zh = caption_units.get(figure_no, (f"Figure {figure_no}", ""))
        return (
            '<div class="figure-with-caption">'
            '<div class="figure-row">'
            f'<img src="{html.escape(figure_paths[figure_no].as_uri())}" '
            f'alt="Figure {figure_no}">'
            "</div>"
            '<div class="caption-row">'
            f'<div class="caption-en">{html.escape(caption_en)}</div>'
            f'<div class="caption-zh">{html.escape(caption_zh)}</div>'
            "</div></div>"
        )

    references_started = False
    for page_no, page in enumerate(pages, 1):
        rows: list[str] = []
        blocks = blocks_by_page[page_no]
        for block_no, block in enumerate(blocks):
            key = (page_no, block_no)
            if figures.get(page_no) and re.fullmatch(r"\([a-z]\)", block.strip(), re.I):
                # Subfigure labels are already present in the rendered figure.
                continue
            references_started = is_reference_block(block, references_started)
            zh = translated[(page_no, block_no)]
            if key in caption_keys:
                pass
            elif is_equation_block(block):
                rows.append(
                    f'<div class="equation-row">{html.escape(block)}</div>'
                )
            else:
                row_class = " pair references" if references_started else " pair"
                rows.append(
                    f'<div class="{row_class}">'
                    f'<div class="cell en" lang="en">{html.escape(block)}</div>'
                    f'<div class="cell zh" lang="zh-CN">{html.escape(zh)}</div>'
                    "</div>"
                )
            for figure_no in anchors.get(key, []):
                rows.append(figure_unit(figure_no))
        page_sections.append(
            f"""
<section class="source-page">
  <div class="source-banner">原论文第 {page_no}/{len(pages)} 页 · Source page {page_no}/{len(pages)}</div>
  <div class="column-labels"><div>English</div><div>中文</div></div>
  {''.join(rows)}
</section>"""
        )

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)} - 中英文对照版</title>
<style>
  @page {{ size: Letter landscape; margin: 8mm 10mm 9mm; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; color: #172033; }}
  body {{ font-family: "Noto Serif CJK SC", "Noto Sans CJK SC", serif; }}
  .cover {{ height: 190mm; break-after: page; display: flex; flex-direction: column; justify-content: center; }}
  .cover h1 {{ margin: 0; padding: 0 6mm 4mm; text-align: center; font: 700 22pt/1.25 "Noto Sans CJK SC", sans-serif; color: #17365d; border-bottom: 1.2px solid #55799e; }}
  .cover h2 {{ margin: 6mm 0 3mm; text-align: center; font: 700 18pt/1.25 "Noto Sans CJK SC", sans-serif; color: #111827; }}
  .cover .meta {{ text-align: center; font: 10pt/1.5 "Noto Sans CJK SC", sans-serif; color: #46566b; }}
  .cover .scope {{ margin: 12mm auto 0; width: 92%; border: 1px solid #b9c7d8; display: grid; grid-template-columns: 1fr 1fr; font-size: 9.5pt; line-height: 1.55; }}
  .cover .scope > div {{ padding: 4mm; }}
  .cover .scope > div + div {{ border-left: 1px solid #b9c7d8; }}
  .source-page {{ break-before: page; }}
  .source-banner {{ margin: 0 0 2.2mm; padding-bottom: 1.5mm; border-bottom: 1px solid #7591ad; text-align: right; font: 700 8pt/1.2 "Noto Sans CJK SC", sans-serif; color: #45627f; }}
  .column-labels {{ display: grid; grid-template-columns: 1fr 1fr; background: #e7eff8; border: 1px solid #aebdce; font: 700 8.5pt/1.25 "Noto Sans CJK SC", sans-serif; }}
  .column-labels > div {{ padding: 2mm 2.2mm; }}
  .column-labels > div + div {{ border-left: 1px solid #aebdce; }}
  .pair {{ display: grid; grid-template-columns: 1fr 1fr; border: 1px solid #c2ccd7; border-top: 0; break-inside: avoid; page-break-inside: avoid; }}
  .figure-row {{ display: flex; justify-content: center; padding: 2.5mm 1mm 1mm; background: white; break-inside: avoid; page-break-inside: avoid; }}
  .figure-row img {{ display: block; max-width: 96%; max-height: 145mm; object-fit: contain; }}
  .figure-with-caption {{ break-inside: avoid; page-break-inside: avoid; }}
  .caption-row {{ padding: 1mm 4mm 2.2mm; text-align: center; break-inside: avoid; page-break-inside: avoid; }}
  .caption-en {{ white-space: pre-wrap; font: italic 8pt/1.35 Georgia, "Times New Roman", serif; }}
  .caption-zh {{ margin-top: .8mm; white-space: pre-wrap; font: 8pt/1.4 "Noto Serif CJK SC", "Noto Sans CJK SC", serif; }}
  .equation-row {{ padding: 1.5mm 4mm; text-align: center; white-space: pre-wrap; font: 10pt/1.35 "Times New Roman", "Noto Serif CJK SC", serif; break-inside: avoid; page-break-inside: avoid; }}
  .cell {{ padding: 1.8mm 2.2mm; white-space: pre-wrap; overflow-wrap: anywhere; }}
  .en {{ font: 8.2pt/1.32 Georgia, "Times New Roman", serif; }}
  .zh {{ border-left: 1px solid #c2ccd7; font: 8.2pt/1.48 "Noto Serif CJK SC", "Noto Sans CJK SC", serif; }}
  .references .cell {{ font-size: 7.5pt; line-height: 1.3; color: #334155; }}
</style>
</head>
<body>
<section class="cover">
  <h1>{html.escape(title)}</h1>
  <h2>{html.escape(title_zh)}</h2>
  <div class="meta">全文中英文逐段对照 · Full English–Chinese Parallel Translation</div>
  <div class="scope">
    <div><strong>English</strong><br>Each figure and bilingual caption follows the first body passage that cites it. Every English passage is aligned with its translation in the same bordered row.</div>
    <div><strong>中文</strong><br>正文首次提及某图后，紧接该原图及中英文图注；每个英文段落与对应中文译文置于同一带边框行中。</div>
  </div>
</section>
{''.join(page_sections)}
</body>
</html>"""
    target.write_text(document, encoding="utf-8")


def build_one(pdf: Path) -> None:
    pages = cached_pages(pdf)
    jobs: list[tuple[int, int, str, bool]] = []
    references_started = False
    for page_no, page in enumerate(pages, 1):
        for block_no, block in enumerate(page_blocks(page["source"])):
            references_started = is_reference_block(block, references_started)
            jobs.append((page_no, block_no, block, references_started))

    print(f"BUILD {pdf.name}: {len(pages)} source pages, {len(jobs)} aligned blocks", flush=True)
    translated: dict[tuple[int, int], str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(translate_block, pdf, block, preserve): (page_no, block_no)
            for page_no, block_no, block, preserve in jobs
        }
        done = 0
        for future in concurrent.futures.as_completed(futures):
            translated[futures[future]] = future.result()
            done += 1
            if done % 25 == 0 or done == len(jobs):
                print(f"  translated/aligned {done}/{len(jobs)} blocks", flush=True)

    output = OUTPUTS / f"{pdf.stem}_中英文对照版.pdf"
    with tempfile.TemporaryDirectory(prefix=f"boxed_{pdf.stem[:2]}_") as tmp_name:
        tmp = Path(tmp_name)
        figures = extract_inline_figures(pdf, tmp)
        html_path = tmp / "boxed.html"
        staged = tmp / "boxed.pdf"
        make_html(pdf, pages, translated, figures, html_path)
        run(
            "google-chrome",
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={staged}",
            html_path.as_uri(),
        )
        if page_count(staged) <= len(pages):
            raise RuntimeError(f"Unexpectedly short boxed PDF: {staged}")
        shutil.copy2(staged, output)
    print(f"DONE {output.name}: {page_count(output)} pages", flush=True)


def main() -> int:
    selected = [
        pdf for pdf in sorted(ORIGINALS.glob("*.pdf")) if pdf.stem[:2] in PAPER_NUMBERS
    ]
    if not selected:
        print("No selected original PDFs found", file=sys.stderr)
        return 1
    for pdf in selected:
        build_one(pdf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
