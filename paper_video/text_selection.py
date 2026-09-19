"""PDF-backed native text ranges; browser offsets use UTF-16 code units."""

import json
import uuid

import pymupdf as fitz

from . import papers


def page_lines(page):
    result = []
    for block in page.get_text("rawdict", sort=True)["blocks"]:
        for line in block.get("lines", []):
            chars = [c for span in line["spans"] for c in span["chars"]]
            text = "".join(c["c"] for c in chars)
            if not text.strip():
                continue
            span = line["spans"][0]
            result.append(
                {
                    "text": text,
                    "bbox": list(line["bbox"]),
                    "size": span["size"],
                    "font": span["font"],
                    "chars": chars,
                }
            )
    return result


def layout(doc_id):
    source = papers.path(doc_id)
    cache = source.parent / "text-layout-v1.json"
    if cache.exists():
        return json.loads(cache.read_text())
    with fitz.open(source) as pdf:
        result = [
            {
                "page": i + 1,
                "width": p.rect.width,
                "height": p.rect.height,
                "lines": [
                    {k: v for k, v in line.items() if k != "chars"}
                    for line in page_lines(p)
                ],
            }
            for i, p in enumerate(pdf)
        ]
    temporary = cache.with_name(uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False))
    temporary.replace(cache)
    return result


def utf16_index(text, offset):
    if type(offset) is not int or offset < 0:
        raise ValueError("字符位置无效")
    count = 0
    for index, char in enumerate(text):
        if count == offset:
            return index
        count += len(char.encode("utf-16-le")) // 2
    if count == offset:
        return len(text)
    raise ValueError("字符位置越界或拆开了 Unicode 字符")


def resolve(doc, text_range):
    start, end = text_range["start"], text_range["end"]

    def key(point):
        values = tuple(point[k] for k in ("page", "line", "offset"))
        if any(type(v) is not int for v in values):
            raise ValueError("文字选区位置必须为整数")
        return values

    if key(start) > key(end):
        start, end = end, start
    if not 1 <= start["page"] <= end["page"] <= doc["page_count"]:
        raise ValueError("选区页码超出论文范围")
    if end["page"] - start["page"] >= 12:
        raise ValueError("一次最多选择连续 12 页，请缩小选区")
    fragments, boxes, pieces = [], {}, []
    with fitz.open(papers.path(doc["id"])) as pdf:
        for number in range(start["page"], end["page"] + 1):
            lines = page_lines(pdf[number - 1])
            first = start["line"] if number == start["page"] else 0
            last = end["line"] if number == end["page"] else len(lines) - 1
            if not lines and number not in (start["page"], end["page"]):
                continue
            if not 0 <= first <= last < len(lines):
                raise ValueError("选区行号无效，请重新选择")
            for index in range(first, last + 1):
                line = lines[index]
                a = (
                    utf16_index(line["text"], start["offset"])
                    if (number, index) == (start["page"], start["line"])
                    else 0
                )
                z = (
                    utf16_index(line["text"], end["offset"])
                    if (number, index) == (end["page"], end["line"])
                    else len(line["text"])
                )
                text = line["text"][a:z]
                if not text:
                    continue
                selected_chars, pos = [], 0
                for char in line["chars"]:
                    next_pos = pos + len(char["c"])
                    if pos < z and next_pos > a:
                        selected_chars.append(char)
                    pos = next_pos
                box = fitz.Rect(selected_chars[0]["bbox"])
                for char in selected_chars[1:]:
                    box |= fitz.Rect(char["bbox"])
                boxes[number] = (boxes[number] | box) if number in boxes else box
                fragments.append(
                    {"page": number, "line": index, "text": text, "bbox": list(box)}
                )
                pieces.append(text)
    text = "\n".join(pieces)
    if not text.strip():
        raise ValueError("请选中至少一个非空白字符")
    if len(text) > 12000:
        raise ValueError("一次最多解读 12000 个字符，请缩小选区")
    first = min(boxes)
    return {
        "id": "text-range",
        "page": first,
        "bbox": list(boxes[first]),
        "text": text,
        "lines": [],
        "fragments": fragments,
        "source_pages": sorted(boxes),
        "page_bboxes": {str(k): list(v) for k, v in boxes.items()},
        "text_range": {"start": start, "end": end},
        "input_mode": "text",
        "context_note": "按实际字符选区解读，已关联所有选中页和论文上下文",
    }


def image_pages(selected, page_count):
    pages = set(selected.get("source_pages", [selected["page"]]))
    pages.update((max(1, min(pages) - 1), min(page_count, max(pages) + 1)))
    return sorted(pages)
