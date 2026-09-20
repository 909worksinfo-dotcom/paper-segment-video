"""Durable chapter jobs. Ordinary selections and their rendering stay unchanged."""

import copy
import hashlib
import json
import logging
import re
import shutil
import threading
import time
import uuid

import imageio_ffmpeg
import jsonschema
import pymupdf as fitz

from . import papers, planner, store, text_selection
from .render import render, timestamp

CHUNK_CHARS = 2600
MAX_PARTS = 64
MAX_SPLIT_DEPTH = 3
MAX_TRIES = 3
LOGGER = logging.getLogger(__name__)
_locks = {}
_locks_guard = threading.Lock()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def headings(doc):
    result = []
    with fitz.open(papers.path(doc["id"])) as pdf:
        for page, sheet in enumerate(pdf, 1):
            for line, row in enumerate(text_selection.page_lines(sheet)):
                match = re.fullmatch(
                    r"\s*(\d+)\.\s+([A-Za-z][^\n]{1,180})\s*", row["text"]
                )
                if match and ("bold" in row["font"].lower() or row["size"] >= 12):
                    result.append(
                        {
                            "chapter": match[1],
                            "title": row["text"].strip(),
                            "page": page,
                            "line": line,
                        }
                    )
    return result


def extract(doc, chapter):
    """Use actual PDF heading/line positions, never an approximate substring match."""
    found = headings(doc)
    matches = [i for i, h in enumerate(found) if h["chapter"] == str(chapter)]
    if len(matches) != 1:
        raise ValueError("章节标题未唯一定位，请核对 chapters 返回的正文标题")
    index = matches[0]
    start = found[index]
    stop = found[index + 1] if index + 1 < len(found) else None
    fragments = []
    with fitz.open(papers.path(doc["id"])) as pdf:
        previous = None
        for number in range(start["page"], (stop["page"] if stop else len(pdf)) + 1):
            sheet = pdf[number - 1]
            for line, row in enumerate(text_selection.page_lines(sheet)):
                if (number, line) < (start["page"], start["line"]):
                    continue
                if stop and (number, line) >= (stop["page"], stop["line"]):
                    break
                # Printed page numbers are not chapter content
                if (
                    row["text"].strip() == str(number)
                    and row["bbox"][1] > sheet.rect.height * 0.9
                ):
                    continue
                section = bool(
                    re.match(r"^\s*\d+(?:\.\d+)+\.?\s+[A-Za-z]", row["text"])
                )
                gap = previous and (
                    previous["page"] != number
                    or row["bbox"][1] - previous["bbox"][3] > row["size"] * 0.7
                )
                fragment = {
                    "page": number,
                    "line": line,
                    "text": row["text"],
                    "bbox": row["bbox"],
                    "boundary": bool(section or gap),
                    "heading": section,
                }
                if re.match(r"^\s*Figure\s+\d+\s*[|:]", row["text"]):
                    images = [
                        fitz.Rect(image["bbox"])
                        for image in sheet.get_image_info()
                        if image["bbox"][3] <= row["bbox"][1] + 4
                    ]
                    if images:
                        fragment["figure_bbox"] = list(
                            max(images, key=lambda box: box.y1)
                        )
                fragments.append(fragment)
                previous = fragment
    if not fragments or sum(len(f["text"]) for f in fragments) > 200000:
        raise ValueError("章节为空或超过 20 万字符，请选择更小的章节")
    return start["title"], fragments


def selection(fragments):
    boxes = {}
    for fragment in fragments:
        page = fragment["page"]
        box = fitz.Rect(fragment["bbox"])
        if fragment.get("figure_bbox"):
            box |= fitz.Rect(fragment["figure_bbox"])
        boxes[page] = boxes[page] | box if page in boxes else box
    first = min(boxes)
    return {
        "id": "chapter",
        "page": first,
        "bbox": list(boxes[first]),
        "text": "\n".join(f["text"] for f in fragments),
        "lines": [],
        "fragments": fragments,
        "source_pages": sorted(boxes),
        "page_bboxes": {str(k): list(v) for k, v in boxes.items()},
        "input_mode": "chapter",
        "context_note": "只讲本段原文；相邻页仅供消歧，按原文顺序合并为章节",
    }


def ranges(fragments, target=CHUNK_CHARS):
    """Contiguous half-open ranges; prefer paragraph/section boundaries."""
    result, begin = [], 0
    while begin < len(fragments):
        end, size, candidates = begin, 0, []
        while end < len(fragments):
            added = len(fragments[end]["text"]) + 1
            if end > begin and size + added > target:
                break
            if end > begin and fragments[end].get("boundary") and size >= target * 0.5:
                candidates.append(end)
            size += added
            end += 1
        if end < len(fragments) and candidates:
            end = candidates[-1]
        end = safe_cut(fragments, begin, end)
        end = max(begin + 1, end)
        result.append((begin, end))
        begin = end
    # Avoid a trailing heading or tiny fragment with no explanatory context
    if (
        len(result) > 1
        and sum(len(f["text"]) for f in fragments[result[-1][0] :]) < 350
    ):
        result[-2:] = [(result[-2][0], result[-1][1])]
    if len(result) > MAX_PARTS:
        raise ValueError("章节超过 64 个分段，请缩小范围")
    return result


def safe_cut(fragments, begin, end):
    """Do not bisect a PDF table row or leave a section heading behind."""
    if end >= len(fragments):
        return end

    def joined(cut):
        a, z = fragments[cut - 1], fragments[cut]
        same_row = (
            "bbox" in a
            and "bbox" in z
            and a.get("page") == z.get("page")
            and abs(a["bbox"][1] - z["bbox"][1]) < 2
        )
        return same_row or a.get("heading")

    original = end
    while end > begin and joined(end):
        end -= 1
    if end > begin:
        return end
    # A single row can exceed the target size; keep it intact and move forward
    end = original
    while end < len(fragments) and joined(end):
        end += 1
    return end


def part(begin, end, depth=0):
    return {
        "id": uuid.uuid4().hex,
        "begin": begin,
        "end": end,
        "depth": depth,
        "state": "pending",
        "tries": 0,
        "attempts": 0,
    }


def coverage(manifest):
    cursor = 0
    for leaf in manifest["parts"]:
        if leaf["begin"] != cursor or not cursor < leaf["end"] <= len(
            manifest["fragments"]
        ):
            raise ValueError("章节分段存在遗漏、重复或顺序错误")
        cursor = leaf["end"]
    if cursor != len(manifest["fragments"]):
        raise ValueError("章节分段未覆盖完整原文")
    if manifest["source_digest"] != digest(manifest["fragments"]):
        raise ValueError("章节原文快照校验失败")
    return True


def repair_pending_boundary(manifest, index, root):
    """Keep an English sentence intact across adjacent unapproved chapter parts."""
    parts, fragments = manifest["parts"], manifest["fragments"]
    if index + 1 >= len(parts):
        return False
    current, following = parts[index : index + 2]
    if current["state"] == "ready" or following["state"] == "ready":
        return False
    if any(
        (root / "parts" / leaf["id"] / "approval.json").exists()
        for leaf in (current, following)
    ):
        return False
    if re.search(r'[.!?][\)\]"”’]*\s*$', fragments[current["end"] - 1]["text"]):
        return False
    # A PDF page or line break can fall inside a long sentence. The next part
    # must retain source text, and the enlarged part stays within the target.
    for boundary in range(current["end"] + 1, following["end"]):
        if sum(len(f["text"]) + 1 for f in fragments[current["begin"] : boundary]) > CHUNK_CHARS:
            break
        if not re.search(r'[.!?][\)\]"”’]*\s*$', fragments[boundary - 1]["text"]):
            continue
        if safe_cut(fragments, current["begin"], boundary) != boundary:
            continue
        current["end"] = boundary
        following["begin"] = boundary
        coverage(manifest)
        return True
    return False


def initialize(directory, title, fragments):
    value = {
        "version": 1,
        "title": title,
        "fragments": fragments,
        "source_digest": digest(fragments),
        "parts": [part(a, z) for a, z in ranges(fragments)],
        "superseded": [],
    }
    coverage(value)
    write_json(directory / "chapter.json", value)
    return value


def split_part(manifest, index):
    leaf = manifest["parts"][index]
    items = manifest["fragments"][leaf["begin"] : leaf["end"]]
    if (
        leaf["depth"] >= MAX_SPLIT_DEPTH
        or len(items) < 4
        or sum(len(f["text"]) for f in items) < 900
        or len(manifest["parts"]) >= MAX_PARTS
    ):
        return False
    total = sum(len(f["text"]) + 1 for f in items)
    half = total / 2
    minimum_child = min(350, total * 0.3)
    candidates, fallback, size = [], [], 0
    for i, f in enumerate(items):
        if 0 < i < len(items) and minimum_child <= size <= total - minimum_child:
            distance = (abs(size - half), i)
            fallback.append(distance)
            if f.get("boundary") or items[i - 1]["text"].rstrip().endswith((".", ":", ";")):
                candidates.append(distance)
        size += len(f["text"]) + 1
    if not fallback:
        return False
    cut = safe_cut(items, 0, min(candidates or fallback)[1])
    left = sum(len(f["text"]) + 1 for f in items[:cut])
    if left < minimum_child or total - left < minimum_child:
        cut = safe_cut(items, 0, min(fallback)[1])
        left = sum(len(f["text"]) + 1 for f in items[:cut])
    if left < minimum_child or total - left < minimum_child:
        return False
    if not 0 < cut < len(items):
        return False
    middle = leaf["begin"] + cut
    manifest["superseded"].append(copy.deepcopy(leaf))
    manifest["parts"][index : index + 1] = [
        part(leaf["begin"], middle, leaf["depth"] + 1),
        part(middle, leaf["end"], leaf["depth"] + 1),
    ]
    coverage(manifest)
    return True


def checked_plan(directory, pages, selected, voice):
    approval = json.loads((directory / "approval.json").read_text())
    plan = json.loads((directory / "plan.json").read_text())
    report = json.loads((directory / "validation.json").read_text())
    if approval != {
        "plan": digest(plan),
        "report": digest(report),
        "selection": digest(selected),
        "speech": digest(voice),
    }:
        raise ValueError("分段缓存指纹不匹配")
    planner.validate_plan(plan, pages, chapter_mode=True, selected=selected)
    review = report.get("scientific_review", {})
    if not (
        review.get("passed")
        and not review.get("issues")
        and not review.get("coverage_gaps")
    ):
        raise ValueError("分段缓存没有完整科学复核记录")
    return plan, report


def previous_review_feedback(directory):
    """Carry the last independent scientific review into an explicit chapter retry."""
    candidates = sorted(
        directory.glob("attempt-*/review-[0-9].json"),
        key=lambda path: (
            int(path.parent.name.split("-")[1]),
            int(path.stem.split("-")[1]),
        ),
    )
    for path in reversed(candidates):
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        issues = review.get("issues", []) + review.get("coverage_gaps", [])
        if issues:
            return (
                "\n同一原文此前未通过独立内容复核，请逐项修正以下问题；"
                "这些是审核反馈而非论文原文，不可据此捏造事实：\n"
                + json.dumps(issues[:12], ensure_ascii=False)[:4000]
            )
    return ""


def ready(directory, leaf):
    output = directory / "lesson.mp4"
    return bool(
        leaf.get("result")
        and leaf.get("media_digest")
        and output.is_file()
        and file_digest(output) == leaf["media_digest"]
        and all(
            (directory / name).is_file()
            for name in (
                "plan.json",
                "validation.json",
                "approval.json",
                "captions.srt",
                "poster.png",
            )
        )
    )


def prepare_retry(job_id):
    file = store.ROOT / "jobs" / job_id / "chapter.json"
    value = json.loads(file.read_text())
    coverage(value)
    for leaf in value["parts"]:
        if leaf["state"] != "ready":
            leaf.update(state="pending", tries=0, error=None)
    write_json(file, value)


def pause(seconds, cancelled):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if cancelled():
            raise planner.Cancelled()
        time.sleep(min(0.2, max(0, until - time.monotonic())))


def _context(doc, selected):
    numbers = set(selected["source_pages"])
    numbers.update(
        (1, max(1, min(numbers) - 1), min(doc["page_count"], max(numbers) + 1))
    )
    return [{"page": n, "text": doc["pages"][n - 1]["text"]} for n in sorted(numbers)]


def _run(job_id):
    job = store.job(job_id)
    root = store.ROOT / "jobs" / job_id
    manifest = json.loads((root / "chapter.json").read_text())
    coverage(manifest)
    doc = store.document(job["document_id"])
    voice = job["speech"]

    def cancelled():
        return store.job(job_id).get("cancel_requested", False)

    def persist():
        write_json(root / "chapter.json", manifest)
        store.update_job(
            job_id,
            chapter_progress={
                "ready": sum(p["state"] == "ready" for p in manifest["parts"]),
                "total": len(manifest["parts"]),
            },
        )

    def progress(state, message):
        if cancelled():
            raise planner.Cancelled()
        store.update_job(
            job_id,
            state=state,
            message=f"章节第 {min(index + 1, len(manifest['parts']))}/{len(manifest['parts'])} 段 · {message}",
        )

    index = 0
    while index < len(manifest["parts"]):
        if cancelled():
            raise planner.Cancelled()
        if repair_pending_boundary(manifest, index, root):
            persist()
        leaf = manifest["parts"][index]
        directory = root / "parts" / leaf["id"]
        directory.mkdir(parents=True, exist_ok=True)
        selected = selection(manifest["fragments"][leaf["begin"] : leaf["end"]])
        pages = _context(doc, selected)
        if (directory / "approval.json").exists():
            try:
                checked_plan(directory, pages, selected, voice)
            except (ValueError, OSError, KeyError, jsonschema.ValidationError):
                # Preserve the invalid evidence; never treat it as an approved plan
                (directory / "approval.json").rename(
                    directory / f"approval.invalid-{uuid.uuid4().hex}.json"
                )
                leaf.update(state="pending", tries=0)
        if leaf["state"] == "ready" and ready(directory, leaf):
            index += 1
            continue
        if leaf["state"] == "ready":
            leaf.update(state="pending", tries=0)

        divided = False
        while leaf["tries"] < MAX_TRIES:
            leaf.update(
                tries=leaf["tries"] + 1, attempts=leaf["attempts"] + 1, state="working"
            )
            persist()
            try:
                if (directory / "approval.json").exists():
                    plan, report = checked_plan(directory, pages, selected, voice)
                else:
                    attempt = directory / f"attempt-{leaf['attempts']}"
                    attempt.mkdir(parents=True, exist_ok=True)
                    write_json(
                        attempt / "context.json",
                        {"selection": selected, "pages": pages},
                    )
                    images = []
                    for number in text_selection.image_pages(
                        selected, doc["page_count"]
                    ):
                        image = attempt / f"source-page-{number}.png"
                        papers.page_image(doc["id"], number, image)
                        images.append(image)
                    plan, report = planner.create_plan(
                        selected,
                        pages,
                        images,
                        attempt,
                        cancelled,
                        progress,
                        chapter_mode=True,
                        initial_feedback=previous_review_feedback(directory),
                    )
                    write_json(directory / "plan.json", plan)
                    write_json(directory / "validation.json", report)
                    write_json(
                        directory / "approval.json",
                        {
                            "plan": digest(plan),
                            "report": digest(report),
                            "selection": digest(selected),
                            "speech": digest(voice),
                        },
                    )
                result = render(
                    doc["id"], selected, plan, directory, cancelled, progress, voice
                )
                if cancelled():
                    raise planner.Cancelled()
                leaf.update(
                    state="ready",
                    result=result,
                    media_digest=file_digest(directory / "lesson.mp4"),
                    error=None,
                )
                persist()
                break
            except planner.Cancelled:
                leaf["state"] = "pending"
                persist()
                raise
            except planner.PlanValidationError as exc:
                leaf.update(state="failed", error=str(exc))
                divided = split_part(manifest, index)
                persist()
                if divided:
                    progress("writing", "审核未通过，已按原文边界细分并保留完整覆盖")
                    break
                if leaf["tries"] >= MAX_TRIES:
                    raise ValueError(
                        f"第 {index + 1} 段连续 {MAX_TRIES} 次内容审核仍未通过；"
                        "已保留已完成片段和审核记录，可从主会话重试"
                    ) from exc
                progress("writing", "审核未通过，正依据复核意见重新编写本段")
                pause(2 ** leaf["tries"], cancelled)
            except Exception as exc:
                leaf.update(state="failed", error=str(exc)[:700])
                persist()
                if leaf["tries"] >= MAX_TRIES:
                    raise RuntimeError(
                        f"第 {index + 1} 段重试 {MAX_TRIES} 次仍失败：{str(exc)[:250]}"
                    ) from exc
                progress(
                    "queued",
                    f"本段失败，{2 ** leaf['tries']} 秒后自动重试；已完成片段保留",
                )
                pause(2 ** leaf["tries"], cancelled)
        if not divided:
            if leaf["state"] != "ready":
                raise RuntimeError(f"第 {index + 1} 段已达到重试上限，请在主会话重试")
            index += 1

    progress("rendering", "全部分段已通过审核，正在合并并核验音画与字幕")
    result, plan, report = assemble(root, manifest, voice, cancelled)
    if cancelled():
        raise planner.Cancelled()
    store.update_job(
        job_id,
        state="ready",
        message="章节视频已就绪 · 含中文配音、动态批注和字幕",
        title=manifest["title"],
        result=result,
        plan=plan,
        validation=report,
        error=None,
    )


def run(job_id):
    with _locks_guard:
        lock = _locks.setdefault(job_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        _run(job_id)
    except planner.Cancelled:
        store.update_job(
            job_id, state="cancelled", message="章节任务已取消，已完成片段保留"
        )
    except Exception as exc:
        LOGGER.exception("Chapter %s failed", job_id)
        store.update_job(
            job_id,
            state="failed",
            message="章节生成未完成，已完成片段保留",
            error=str(exc)[:700],
        )
    finally:
        lock.release()


def parse_srt(path):
    def seconds(value):
        h, m, s = value.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            raise ValueError("分段字幕格式损坏")
        a, z = [seconds(x) for x in lines[1].split(" --> ")]
        if not 0 <= a < z:
            raise ValueError("分段字幕时间无效")
        cues.append((a, z, "\n".join(lines[2:])))
    return cues


def assemble(root, manifest, voice, cancelled):
    coverage(manifest)
    plan = {
        "title": manifest["title"],
        "takeaway": "按论文原文顺序完成章节精读",
        "knowledge_points": [],
        "scenes": [],
        "limitations": [],
        "check_question": "",
        "check_answer": "",
    }
    result = {
        "duration": 0,
        "timeline": [],
        "flow_steps": [],
        "video": "lesson.mp4",
        "audio_verified": False,
        "video_verified": False,
        "speech_speed": voice["speed"],
        "speech_provider": voice["provider"],
        "speech_label": voice["label"],
    }
    reports, subtitles, listing, elapsed = [], [], [], 0.0
    for i, leaf in enumerate(manifest["parts"]):
        if cancelled():
            raise planner.Cancelled()
        directory = root / "parts" / leaf["id"]
        if leaf["state"] != "ready" or not ready(directory, leaf):
            raise ValueError("存在未完成或损坏的分段，禁止合并")
        p = json.loads((directory / "plan.json").read_text())
        report = json.loads((directory / "validation.json").read_text())
        selected = selection(manifest["fragments"][leaf["begin"] : leaf["end"]])
        approval = json.loads((directory / "approval.json").read_text())
        if approval != {
            "plan": digest(p),
            "report": digest(report),
            "selection": digest(selected),
            "speech": digest(voice),
        }:
            raise ValueError("分段审核指纹已变化，禁止合并")
        review = report.get("scientific_review", {})
        if (
            not review.get("passed")
            or review.get("issues")
            or review.get("coverage_gaps")
        ):
            raise ValueError("存在未通过科学复核的分段，禁止合并")
        offset = len(plan["scenes"])
        for point in p["knowledge_points"]:
            plan["knowledge_points"].append(
                {**point, "id": f"part{i + 1}-{point['id']}"}
            )
        for scene in p["scenes"]:
            plan["scenes"].append(
                {
                    **scene,
                    "knowledge_ids": [
                        f"part{i + 1}-{k}" for k in scene["knowledge_ids"]
                    ],
                }
            )
        plan["limitations"].extend(p["limitations"])
        plan.update(check_question=p["check_question"], check_answer=p["check_answer"])
        for item in leaf["result"]["timeline"]:
            result["timeline"].append(
                {
                    **item,
                    "index": item["index"] + offset,
                    "start": round(elapsed + item["start"], 3),
                    "end": round(elapsed + item["end"], 3),
                }
            )
        for item in leaf["result"].get("flow_steps", []):
            result["flow_steps"].append(
                {
                    **item,
                    "scene": item["scene"] + offset,
                    "start": round(elapsed + item["start"], 3),
                    "pause_at": round(elapsed + item["pause_at"], 3),
                }
            )
        duration = leaf["result"]["duration"]
        for a, z, text in parse_srt(directory / "captions.srt"):
            if z > duration + 0.1:
                raise ValueError("分段字幕超出视频时长")
            subtitles.append((elapsed + a, elapsed + z, text))
        listing.extend(
            [f"file 'parts/{leaf['id']}/lesson.mp4'", f"duration {duration:.6f}"]
        )
        reports.append({"part": leaf["id"], "validation": report})
        elapsed += duration
    plan["limitations"] = list(dict.fromkeys(plan["limitations"]))
    concat = root / "concat.txt"
    concat.write_text("\n".join(listing), encoding="utf-8")
    output = root / "lesson.partial.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    planner.run_process(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(concat),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        root,
        root / "concat.log",
        cancelled,
        900,
    )
    planner.run_process(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(output),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        root,
        root / "verify.log",
        cancelled,
        max(900, elapsed * 2),
    )
    reader = imageio_ffmpeg.read_frames(str(output))
    try:
        metadata = next(reader)
    finally:
        reader.close()
    if abs(metadata["duration"] - elapsed) > max(0.5, len(manifest["parts"]) * 0.05):
        raise ValueError("合并视频的时长与章节时间轴不一致")
    (root / "captions.srt").write_text(
        "\n\n".join(
            f"{i + 1}\n{timestamp(a, True)} --> {timestamp(z, True)}\n{text}"
            for i, (a, z, text) in enumerate(subtitles)
        ),
        encoding="utf-8",
    )
    (root / "captions.vtt").write_text(
        "WEBVTT\n\n"
        + "\n\n".join(
            f"{timestamp(a)} --> {timestamp(z)}\n{text}" for a, z, text in subtitles
        ),
        encoding="utf-8",
    )
    shutil.copyfile(
        root / "parts" / manifest["parts"][0]["id"] / "poster.png", root / "poster.png"
    )
    total = len(plan["knowledge_points"])
    report = {
        "citations_located": True,
        "knowledge_points": total,
        "covered_points": total,
        "scenes": len(plan["scenes"]),
        "scientific_review": {
            "passed": True,
            "issues": [],
            "coverage_gaps": [],
            "basis": "每个分段的独立科学复核均通过",
        },
        "source_coverage": {
            "complete": True,
            "fragments": len(manifest["fragments"]),
            "digest": manifest["source_digest"],
        },
        "parts": reports,
    }
    write_json(root / "plan.json", plan)
    write_json(root / "validation.json", report)
    output.replace(root / "lesson.mp4")
    result.update(
        duration=round(elapsed, 3),
        audio_verified=True,
        video_verified=True,
        parts=len(manifest["parts"]),
    )
    return result, plan, report
