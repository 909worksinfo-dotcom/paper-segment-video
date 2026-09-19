"""Deterministic paper visuals and locally synthesized speech, on one timeline."""

import math
import json
import os
import re
import subprocess
import wave
import unicodedata
from functools import lru_cache
from pathlib import Path

import imageio_ffmpeg
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

from .papers import path, quote_marks
from .planner import Cancelled, run_process
from . import speech, flow

W, H, FPS = 1280, 720, 10
INK, MUTED, GREEN, PAPER = "#182d28", "#68746c", "#187451", "#f4f1e9"


@lru_cache(maxsize=32)
def font(size):
    candidates = [
        os.environ.get("PAPER_VIDEO_FONT", ""),
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise RuntimeError("缺少中文字体，请设置 PAPER_VIDEO_FONT 指向中文 TTF/TTC")


def readable_formula(text):
    """Keep index semantics in linear notation, avoiding unsupported modifier glyphs."""
    output, i = [], 0
    while i < len(text):
        char = text[i]
        if char in "₍₎":
            output.append("[" if char == "₍" else "]")
            i += 1
            continue
        name = unicodedata.name(char, "")
        kind = (
            "sub"
            if "SUBSCRIPT" in name
            else "super"
            if ("SUPERSCRIPT" in name or "MODIFIER LETTER" in name)
            else None
        )
        if kind:
            part = []
            while i < len(text):
                current = text[i]
                name = unicodedata.name(current, "")
                current_kind = (
                    "sub"
                    if "SUBSCRIPT" in name
                    else "super"
                    if ("SUPERSCRIPT" in name or "MODIFIER LETTER" in name)
                    else None
                )
                if current in "₍₎" or current_kind != kind:
                    break
                part.append(unicodedata.normalize("NFKC", current))
                i += 1
            output.append(
                ("[" if kind == "sub" else "^(")
                + "".join(part)
                + ("]" if kind == "sub" else ")")
            )
        else:
            output.append(unicodedata.normalize("NFKC", char))
            i += 1
    value = "".join(output).replace(";", ";\n")
    missing = bytes(font(22).getmask(chr(0x10FFFF)))
    if any(not c.isspace() and bytes(font(22).getmask(c)) == missing for c in value):
        return "完整公式请看左侧原文"
    return value


def wrap(text, size, width):
    f = font(size)
    rows, row = [], ""
    for char in text:
        if char == "\n" or (row and f.getlength(row + char) > width):
            rows.append(row)
            row = "" if char == "\n" else char
        else:
            row += char
    if row:
        rows.append(row)
    return rows


def write(draw, xy, text, size=24, color=INK, width=600, leading=1.45):
    x, y = xy
    for row in wrap(text, size, width):
        draw.text((x, y), row, font=font(size), fill=color)
        y += int(size * leading)
    return y


def subtitle_chunks(narration):
    pieces = re.split(r"(?<=[。！？；])", narration)
    result = []
    for piece in pieces:
        if not piece.strip():
            continue
        # Character wrapping does not drop any narration characters
        result.extend(wrap(piece, 25, 1130))
    return result


def background(doc_id, selected, scene, index, count, title):
    frame = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(frame)
    d.rectangle((0, 0, W, 7), fill=GREEN)
    write(d, (38, 25), "PAPER / 逐段精读", 20, GREEN)
    write(
        d,
        (1010, 27),
        f"{index + 1:02d} / {count:02d}   ·   第 {scene['source_page']} 页",
        17,
        MUTED,
        240,
    )
    write(
        d, (38, 71), scene["title"], 34 if len(scene["title"]) < 30 else 27, INK, 1180
    )
    # Source image: crop around the evidence, retaining adjacent context
    selected_box = selected.get("page_bboxes", {}).get(str(scene["source_page"]))
    if selected_box is None and scene["source_page"] == selected["page"]:
        selected_box = selected["bbox"]
    marks = quote_marks(
        doc_id,
        scene["source_page"],
        scene["highlight_quote"],
        selected_box,
    ) if selected_box is not None else []
    # Missing, ambiguous or out-of-selection quotes leave the source unmarked
    rects = [mark["rect"] for mark in marks]
    with fitz.open(path(doc_id)) as pdf:
        page = pdf[scene["source_page"] - 1]
        if selected_box is not None:
            focus = fitz.Rect(selected_box)
        elif rects:
            focus = fitz.Rect(rects[0])
            for block in page.get_text("blocks"):
                if block[6] == 0 and fitz.Rect(block[:4]).intersects(focus):
                    focus |= fitz.Rect(block[:4])
        else:
            focus = page.rect
        for r in rects:
            focus |= fitz.Rect(r)
        if scene["kind"] == "formula":
            # Include nearby typeset math blocks, often split out by superscripts/subscripts
            neighbors = fitz.Rect(
                0, focus.y0 - 12, page.rect.width, min(page.rect.height, focus.y1 + 65)
            )
            for block in page.get_text("blocks"):
                if block[6] == 0 and fitz.Rect(block[:4]).intersects(neighbors):
                    focus |= fitz.Rect(block[:4])
        # Keep complete text lines: a short quote must not crop away the rest of its paragraph
        x0, x1 = (
            max(0, min(35, focus.x0 - 5)),
            min(page.rect.x1, max(page.rect.x1 - 35, focus.x1 + 5)),
        )
        if page.rect.width < 100:
            x0, x1 = page.rect.x0, page.rect.x1
        y0, y1 = max(0, focus.y0 - 6), min(page.rect.y1, focus.y1 + 6)
        clip = fitz.Rect(x0, y0, x1, max(y1, y0 + 20))
        scale = min(710 / clip.width, 415 / clip.height, 3)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        picture = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    bx, by = 38, 150
    d.rounded_rectangle((bx, by, 786, 591), radius=12, fill="white", outline="#d9dfd6")
    px, py = bx + (748 - picture.width) // 2, by + (441 - picture.height) // 2
    frame.paste(picture, (px, py))
    mapped = []
    for mark in marks:
        x0, y0, x1, _ = mark["rect"]
        # Pixmap origin is rounded to device pixels; clip.x0/y0 are not
        mapped.append(
            (
                max(px, px + x0 * scale - pix.x),
                py + y0 * scale - pix.y,
                min(px + picture.width, px + x1 * scale - pix.x),
                py + mark["underline_y"] * scale - pix.y,
            )
        )
    d = ImageDraw.Draw(frame)
    labels = {
        "motivation": "先看问题",
        "concept": "拆开理解",
        "formula": "公式拆解 · 原式见左",
        "architecture": "追踪信息流",
        "comparison": "比较与证据",
        "limits": "边界与代价",
        "recap": "回顾与自测",
    }
    write(d, (817, 153), labels[scene["kind"]], 17, GREEN, 400)
    y = 195
    for bullet in [] if flow.enabled(scene) else scene["bullets"]:
        d.ellipse((819, y + 7, 825, y + 13), fill=GREEN)
        y = write(d, (838, y), bullet, 19, INK, 386) + 12
    if scene["formula"] and not flow.enabled(scene):
        y += 8
        formula = readable_formula(scene["formula"])
        formula_rows = wrap(formula, 22, 370)
        needed = len(formula_rows) * 32 + 24
        if y + needed > 579:
            raise ValueError("公式场景文字超出画面，请缩短要点")
        d.rounded_rectangle((811, y, 1242, y + needed), radius=8, fill="#e1e9df")
        write(d, (829, y + 12), formula, 22, INK, 385)
        y += needed + 16
    if scene["diagram_nodes"] and not scene["formula"] and not flow.enabled(scene):
        y += 2
        for node_index, node in enumerate(scene["diagram_nodes"]):
            if y + 38 > 588:
                raise ValueError("架构场景节点超出画面，请缩短要点")
            d.rounded_rectangle((826, y, 1226, y + 34), radius=6, fill="#e1e9df")
            write(d, (839, y + 6), node, 17, INK, 370)
            if node_index < len(scene["diagram_nodes"]) - 1:
                d.line((1026, y + 35, 1026, y + 41), fill=GREEN, width=1)
                d.polygon([(1023, y + 39), (1029, y + 39), (1026, y + 42)], fill=GREEN)
            y += 43
    if y > 601:
        raise ValueError("场景排版溢出，请缩短要点")
    d.rectangle((0, 613, W, H), fill=INK)
    write(d, (39, 591), "原文定位 · 黄色下划线随讲解推进", 12, MUTED, 730)
    return frame, mapped


def synthesize(text, directory, index, cancelled, settings=None):
    settings = settings or speech.profile()
    prefix = directory / f"scene-{index:02d}"
    txt = prefix.with_suffix(".txt")
    raw = prefix.with_suffix(
        ".raw.mp3"
        if settings["provider"] == "edge-neural"
        else ".raw.wav"
        if settings["provider"] == "qwen-reference"
        else ".aiff"
    )
    wav = prefix.with_suffix(".wav")
    txt.write_text(text, encoding="utf-8")
    tts_input = prefix.with_suffix(".speech.txt")
    tts_input.write_text(speech.spoken_text(text, settings), encoding="utf-8")
    run_process(
        speech.command(settings, tts_input, raw),
        directory,
        prefix.with_suffix(".tts.log"),
        cancelled,
        900,
    )
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    run_process(
        [
            ffmpeg,
            "-y",
            "-i",
            str(raw),
            "-af",
            "atempo="
            + str(1.0 if settings["provider"] == "edge-neural" else settings["speed"]),
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ],
        directory,
        prefix.with_suffix(".audio.log"),
        cancelled,
        60,
    )
    with wave.open(str(wav)) as audio:
        duration = audio.getnframes() / audio.getframerate()
        samples = audio.readframes(audio.getnframes())
    if duration < 0.5 or not any(samples):
        raise RuntimeError("语音合成结果为空")
    metadata = raw.with_suffix(".timing.json")
    if settings["provider"] == "edge-neural" and metadata.exists():
        prefix.with_suffix(".timings.json").write_text(
            metadata.read_text(), encoding="utf-8"
        )
    raw.unlink(missing_ok=True)
    return wav, duration


def timestamp(seconds, comma=False):
    ms = round(seconds * 1000)
    h, rest = divmod(ms, 3600000)
    m, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{ms:03d}"


def highlight_segments(highlights, seconds):
    """Trace at a constant visual speed; finish even a multiline quote in <=1.65s."""
    total = sum(max(0, x1 - x0) for x0, _, x1, _ in highlights)
    duration = min(1.5, max(0.45, total / 650))
    distance = max(0, min(1, (seconds - 0.15) / duration)) * total
    segments = []
    for x0, _, x1, baseline in highlights:
        length = max(0, x1 - x0)
        traced = min(length, max(0, distance))
        if traced:
            segments.append((x0, baseline, x0 + traced, traced < length))
        distance -= length
    return segments


def draw_highlights(draw, highlights, seconds):
    for x0, baseline, endx, moving in highlight_segments(highlights, seconds):
        draw.line((x0, baseline, endx, baseline), fill="#e9af24", width=3)
        if moving:
            draw.ellipse(
                (endx - 3, baseline - 3, endx + 3, baseline + 3), fill="#db673c"
            )


def render(doc_id, selected, plan, directory, cancelled, progress, settings=None):
    settings = settings or speech.profile()
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    clips, timeline, subtitles, elapsed = [], [], [], 0.0
    flow_timeline = []
    for index, scene in enumerate(plan["scenes"]):
        if cancelled():
            raise Cancelled()
        progress(
            "rendering", f"合成第 {index + 1}/{len(plan['scenes'])} 段配音与动态批注"
        )
        wav, speech_duration = synthesize(
            scene["narration"], directory, index, cancelled, settings
        )
        duration = math.ceil(speech_duration * FPS) / FPS
        timing_file = directory / f"scene-{index:02d}.timings.json"
        boundaries = (
            json.loads(timing_file.read_text())
            if timing_file.exists() and settings["provider"] == "edge-neural"
            else []
        )
        flow_times = flow.schedule(scene, speech_duration, boundaries)
        for step_index, (at, step) in enumerate(
            zip(flow_times, scene.get("flow", {}).get("steps", []))
        ):
            following = (
                flow_times[step_index + 1]
                if step_index + 1 < len(flow_times)
                else speech_duration
            )
            flow_timeline.append(
                {
                    "scene": index,
                    "step": step_index,
                    "start": round(elapsed + at, 3),
                    "pause_at": round(elapsed + min(at + 1.0, following - 0.05), 3),
                    "title": step["action"],
                    "input": step["input"],
                    "output": step["output"],
                    "condition": step["condition"],
                    "evidence": step["evidence"],
                    "alignment": "word-boundary" if boundaries else "estimated",
                }
            )
        frame, highlights = background(
            doc_id, selected, scene, index, len(plan["scenes"]), plan["title"]
        )
        chunks = subtitle_chunks(scene["narration"])
        weight = sum(len(c) for c in chunks)
        timed, pos = [], 0.0
        for chunk in chunks:
            end = pos + speech_duration * len(chunk) / weight
            timed.append((pos, end, chunk))
            subtitles.append((elapsed + pos, elapsed + end, chunk))
            pos = end
        clip = directory / f"scene-{index:02d}.mp4"
        command = [
            ffmpeg,
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{W}x{H}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-i",
            str(wav),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-af",
            "apad",
            "-t",
            str(duration),
            "-movflags",
            "+faststart",
            str(clip),
        ]
        with open(directory / f"scene-{index:02d}.render.log", "wb") as log:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=log, stderr=log
            )
            try:
                for tick in range(round(duration * FPS)):
                    if cancelled():
                        raise Cancelled()
                    current = frame.copy()
                    d = ImageDraw.Draw(current)
                    t = tick / FPS
                    draw_highlights(d, highlights, t)
                    if flow.enabled(scene):
                        flow.draw_graph(current, scene, t, flow_times, font)
                    flow.cursor(
                        ImageDraw.Draw(current),
                        flow.guide_point(
                            scene,
                            t,
                            speech_duration,
                            highlights,
                            font,
                            flow_times,
                            boundaries,
                        ),
                        t,
                    )
                    subtitle = next(
                        (text for begin, end, text in timed if begin <= t < end),
                        chunks[-1],
                    )
                    write(d, (42, 641), subtitle, 25, "#ffffff", 1195)
                    # This is the current chapter's progress, reset at each chapter boundary
                    d.rectangle(
                        (0, H - 4, int(W * t / max(duration, 1)), H), fill="#d9ac54"
                    )
                    process.stdin.write(current.tobytes())
                    if tick == min(15, round(duration * FPS) - 1):
                        current.save(directory / f"scene-{index:02d}.png")
                        if index == 0:
                            current.save(directory / "poster.png")
                process.stdin.close()
                code = process.wait(timeout=90)
                if code:
                    raise RuntimeError(f"视频编码失败：第 {index + 1} 场景")
            except BaseException:
                process.kill()
                process.wait()
                raise
        timeline.append(
            {
                "index": index,
                "start": elapsed,
                "end": elapsed + duration,
                "title": scene["title"],
                "source_page": scene["source_page"],
            }
        )
        elapsed += duration
        clips.append(clip)
    listing = directory / "concat.txt"
    listing.write_text("\n".join(f"file '{clip.name}'" for clip in clips))
    output = directory / "lesson.mp4"
    run_process(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        directory,
        directory / "concat.log",
        cancelled,
        180,
    )
    # Decode the finished container to catch truncated streams, not just file existence
    run_process(
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
        directory,
        directory / "verify.log",
        cancelled,
        180,
    )
    (directory / "captions.vtt").write_text(
        "WEBVTT\n\n"
        + "\n\n".join(
            f"{timestamp(a)} --> {timestamp(b)}\n{text}" for a, b, text in subtitles
        ),
        encoding="utf-8",
    )
    (directory / "captions.srt").write_text(
        "\n\n".join(
            f"{i + 1}\n{timestamp(a, True)} --> {timestamp(b, True)}\n{text}"
            for i, (a, b, text) in enumerate(subtitles)
        ),
        encoding="utf-8",
    )
    return {
        "duration": round(elapsed, 2),
        "timeline": timeline,
        "flow_steps": flow_timeline,
        "video": "lesson.mp4",
        "audio_verified": True,
        "video_verified": True,
        "speech_speed": settings["speed"],
        "speech_provider": settings["provider"],
        "speech_label": settings["label"],
    }
