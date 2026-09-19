"""Preserve the PDF as the visual authority; coordinates are PDF points."""

import hashlib
import re
import unicodedata
import uuid

import pymupdf as fitz

from . import store


def normal(text):
    # Join typesetting hyphenation only at a physical line break, not semantic hyphens
    text = re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).replace("\u00ad", "")


def ingest(data: bytes, filename: str):
    if not data.startswith(b"%PDF-"):
        raise ValueError("请选择有效的 PDF 文件")
    if len(data) > 100 * 1024 * 1024:
        raise ValueError("Demo 单个文件上限为 100 MB")
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("PDF 已损坏或无法解析") from exc
    with pdf:
        if pdf.needs_pass:
            raise ValueError("请先解除 PDF 密码保护后再导入")
        if not 1 <= len(pdf) <= 300:
            raise ValueError("Demo 支持 1–300 页的论文")
        doc_id = hashlib.sha256(data).hexdigest()[:24]
        pages = []
        for index, page in enumerate(pdf):
            if page.rotation:
                # Normalize the internal working copy while preserving the visual appearance
                page.remove_rotation()
            blocks = []
            for number, block in enumerate(page.get_text("dict", sort=True)["blocks"]):
                if block["type"] != 0:
                    continue
                lines = []
                for line in block["lines"]:
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if text:
                        lines.append({"text": text, "bbox": list(line["bbox"])})
                text = "\n".join(x["text"] for x in lines)
                if text:
                    blocks.append(
                        {
                            "id": f"p{index + 1}-b{number}",
                            "text": text,
                            "bbox": list(block["bbox"]),
                            "lines": lines,
                        }
                    )
            pages.append(
                {
                    "number": index + 1,
                    "width": page.rect.width,
                    "height": page.rect.height,
                    "text": page.get_text(sort=True),
                    "blocks": blocks,
                }
            )
        title = (pdf.metadata or {}).get("title") or filename.rsplit(".", 1)[0]
        result = {
            "id": doc_id,
            "title": title,
            "filename": filename,
            "pages": pages,
            "page_count": len(pages),
            "text_pages": sum(bool(p["blocks"]) for p in pages),
        }
        working_copy = pdf.tobytes(garbage=3, deflate=True)
    directory = store.ROOT / "papers" / doc_id
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in [("uploaded.pdf", data), ("source.pdf", working_copy)]:
        temporary = directory / (uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(directory / name)
    store.save_document(result)
    return result


def path(doc_id):
    store.document(doc_id)  # Require a known identifier before touching a path
    return store.ROOT / "papers" / doc_id / "source.pdf"


def selection(doc, page_number, block_id=None, bbox=None):
    if not 1 <= page_number <= len(doc["pages"]):
        raise ValueError("页码超出范围")
    page = doc["pages"][page_number - 1]
    if block_id:
        block = next((b for b in page["blocks"] if b["id"] == block_id), None)
        if not block:
            raise ValueError("段落不属于当前页")
        return {"page": page_number, **block}
    if bbox is None or len(bbox) != 4:
        raise ValueError("请选择一个段落或框选图表、公式区域")
    x0, y0, x1, y1 = bbox
    if not (
        0 <= x0 < x1 <= page["width"]
        and 0 <= y0 < y1 <= page["height"]
        and x1 - x0 >= 8
        and y1 - y0 >= 8
    ):
        raise ValueError("框选区域无效，请在论文页内重新选择")
    with fitz.open(path(doc["id"])) as pdf:
        text = pdf[page_number - 1].get_textbox(fitz.Rect(bbox)).strip()
    return {
        "id": "region",
        "page": page_number,
        "bbox": bbox,
        "text": text,
        "lines": [],
    }


def context(doc, selected):
    # Always include the selected page, neighbors, opening, conclusion, and lexical matches
    n = selected["page"] - 1
    mandatory = {0, n, max(0, n - 1), min(len(doc["pages"]) - 1, n + 1)}
    mandatory.update(p - 1 for p in selected.get("source_pages", []))
    for i, page in enumerate(doc["pages"]):
        if re.search(r"(?im)^\s*\d*[.\s]*Conclusion", page["text"]):
            mandatory.add(i)
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", selected["text"].lower()))
    stop = {
        "that",
        "with",
        "from",
        "this",
        "have",
        "model",
        "which",
        "their",
        "these",
        "tokens",
        "attention",
    }
    words -= stop
    scored = sorted(
        range(len(doc["pages"])),
        key=lambda i: sum(
            min(doc["pages"][i]["text"].lower().count(w), 4) for w in words
        ),
        reverse=True,
    )
    ids = set(mandatory)
    for i in scored:
        if len(ids) >= 14:
            break
        ids.add(i)
    return [{"page": i + 1, "text": doc["pages"][i]["text"]} for i in sorted(ids)]


def page_image(doc_id, number, output, max_width=1400):
    with fitz.open(path(doc_id)) as pdf:
        if not 1 <= number <= len(pdf):
            raise ValueError("页码超出范围")
        page = pdf[number - 1]
        scale = min(max_width / page.rect.width, 1800 / page.rect.height, 3)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        pix.save(str(output))


def quote_marks(doc_id, number, quote, preferred=None):
    """Locate an unambiguous occurrence inside the selection, never a nearby fallback."""
    needle = normal(quote)
    if not needle:
        return []
    with fitz.open(path(doc_id)) as pdf:
        page = pdf[number - 1]
        raw, glyphs = [], []
        line_id = 0
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for char in span["chars"]:
                        raw.append(char["c"])
                        glyphs.extend(
                            [(line_id, char["bbox"], char["origin"][1], span["size"])]
                            * len(char["c"])
                        )
                raw.append("\n")
                glyphs.append(None)
                line_id += 1
        raw = "".join(raw)
        skipped = set()
        for match in re.finditer(r"(?<=\w)-[ \t]*\n\s*(?=\w)", raw):
            skipped.update(range(match.start(), match.end()))
        normalized, mapping = [], []
        for index, char in enumerate(raw):
            if index in skipped:
                continue
            for value in unicodedata.normalize("NFKC", char):
                if not value.isspace() and value != "\u00ad":
                    normalized.append(value)
                    mapping.append(glyphs[index])
        haystack = "".join(normalized)
        occurrences, start = [], 0
        while (start := haystack.find(needle, start)) >= 0:
            lines = {}
            for glyph in mapping[start : start + len(needle)]:
                if glyph is None:
                    continue
                line, box, baseline, size = glyph
                if line not in lines:
                    lines[line] = {
                        "rect": fitz.Rect(box),
                        "underline_y": baseline + size * 0.12,
                    }
                else:
                    lines[line]["rect"] |= fitz.Rect(box)
                    lines[line]["underline_y"] = max(
                        lines[line]["underline_y"], baseline + size * 0.12
                    )
            if lines:
                occurrences.append(list(lines.values()))
            start += len(needle)
        if not occurrences:
            return []
        anchor = fitz.Rect(preferred) if preferred is not None else None
        if anchor is not None:
            # A small tolerance accommodates rounded PDF selection coordinates
            anchor = anchor + (-2, -2, 2, 2)
            occurrences = [
                marks for marks in occurrences
                if all(anchor.contains(mark["rect"]) for mark in marks)
            ]
        if len(occurrences) != 1:
            return []
        best = occurrences[0]
        return [{**mark, "rect": list(mark["rect"])} for mark in best]


def quote_rects(doc_id, number, quote):
    return [mark["rect"] for mark in quote_marks(doc_id, number, quote)]


def pasted_selection(text, document_id=None):
    """Resolve pasted prose exactly; ambiguous or missing matches stay self-contained."""
    text = text.strip()
    if not 20 <= len(text) <= 12000:
        raise ValueError("请粘贴 20–12000 个字符的论文原文")
    needle = normal(text)
    if len(needle) < 20:
        raise ValueError("有效原文至少需要 20 个非空白字符")
    candidates = [store.document(document_id)] if document_id else store.documents()
    matches = []
    ambiguous = False
    for doc in candidates:
        if doc.get("source_kind") == "pasted":
            continue
        blocks = [
            (page["number"], block) for page in doc["pages"] for block in page["blocks"]
        ]
        spans, offset = [], 0
        for number, block in blocks:
            length = len(normal(block["text"]))
            spans.append((offset, offset + length, number, block))
            offset += length
        haystack = "".join(normal(block["text"]) for _, block in blocks)
        start = haystack.find(needle)
        if start < 0:
            continue
        if haystack.find(needle, start + 1) >= 0:
            ambiguous = True
            continue
        touched = [
            (n, b) for a, z, n, b in spans if a < start + len(needle) and z > start
        ]
        if not touched:
            continue
        number = touched[0][0]
        first_page = [b for n, b in touched if n == number]
        box = fitz.Rect(first_page[0]["bbox"])
        for block in first_page[1:]:
            box |= fitz.Rect(block["bbox"])
        matches.append(
            (
                doc,
                {
                    "id": "pasted-match",
                    "page": number,
                    "bbox": list(box),
                    "text": text,
                    "lines": [],
                    "source_pages": sorted({n for n, _ in touched}),
                    "input_mode": "chat",
                    "context_note": "已匹配导入论文，结合相关页面解读",
                },
            )
        )
    if len(matches) == 1 and not ambiguous:
        return matches[0]
    # A generated source sheet preserves the pasted words; it is never described as the original PDF
    pdf = fitz.open()
    font = fitz.Font("china-s")
    page = pdf.new_page(width=612, height=792)
    y, row = 54, ""
    rows = []
    for char in text:
        if char == "\n" or (row and font.text_length(row + char, fontsize=12) > 516):
            rows.append(row)
            row = "" if char == "\n" else char
        else:
            row += char
    if row:
        rows.append(row)
    for row in rows:
        if y > 738:
            page = pdf.new_page(width=612, height=792)
            y = 54
        page.insert_text((48, y), row, fontname="china-s", fontsize=12)
        y += 20
    data = pdf.tobytes(no_new_id=True)
    pdf.close()
    doc = ingest(data, "主会话粘贴原文.pdf")
    doc.update(
        source_kind="pasted", original_text=text, title="主会话原文 · " + text[:36]
    )
    store.save_document(doc)
    first = doc["pages"][0]
    if not first["blocks"]:
        raise ValueError("该段文字无法排版，请提供可复制的论文原文")
    box = fitz.Rect(first["blocks"][0]["bbox"])
    for block in first["blocks"][1:]:
        box |= fitz.Rect(block["bbox"])
    return doc, {
        "id": "pasted-text",
        "page": 1,
        "bbox": list(box),
        "text": text,
        "lines": [],
        "source_pages": list(range(1, doc["page_count"] + 1)),
        "input_mode": "chat",
        "context_note": "仅有粘贴片段；页码是片段排版页，不是论文页码。未提供的公式、图表、实验及比较结论不得编造",
    }
