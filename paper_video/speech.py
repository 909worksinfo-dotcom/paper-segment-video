"""Explicit speech profiles; a failed reference voice never silently changes voice."""

import json
import os
import re
import sys
from pathlib import Path

RUNTIME = Path.home() / ".local/share/paper-segment-video"


def profile():
    config = RUNTIME / "voice/config.json"
    if config.exists():
        value = json.loads(config.read_text())
        if value.get("provider") == "edge-neural":
            voice = value.get("voice", "zh-CN-YunyangNeural")
            if voice not in {
                "zh-CN-YunxiNeural",
                "zh-CN-YunyangNeural",
                "zh-CN-YunjianNeural",
            }:
                raise ValueError("未知中文神经男声")
            return {
                "provider": "edge-neural",
                "voice": voice,
                "speed": 1.0,
                "rate": "+0%",
                "label": (
                    "云扬 · 沉稳男声"
                    if voice == "zh-CN-YunyangNeural"
                    else "中文神经男声"
                )
                + " · AI 合成讲解 · 1×",
                "remote_narration": True,
            }
        if value.get("provider") != "qwen-reference":
            raise ValueError("未知配音 provider")
        for key in ("python", "model", "reference", "transcript"):
            if not Path(value[key]).exists():
                raise ValueError(f"参考音色配置缺少 {key} 文件")
        return {**value, "speed": 1.0, "label": "李沐参考音色 · AI 合成讲解 · 1×"}
    return {
        "provider": "macos",
        "voice": os.environ.get("PAPER_VIDEO_VOICE", "Tingting"),
        "speed": 1.0,
        "label": "系统中文配音 · 课堂语气 · AI 合成讲解 · 1×",
        "prosody": "classroom-v1",
    }


def command(settings, text_file, output):
    if settings["provider"] == "edge-neural":
        return [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "scripts/neural_voice.py"),
            "--text-file",
            str(text_file),
            "--output",
            str(output),
            "--voice",
            settings["voice"],
            "--rate=" + settings.get("rate", "+0%"),
        ]
    if settings["provider"] == "qwen-reference":
        return [
            settings["python"],
            str(Path(__file__).resolve().parent.parent / "scripts/voice.py"),
            "--text-file",
            str(text_file),
            "--output",
            str(output),
            "--model",
            settings["model"],
            "--reference",
            settings["reference"],
            "--transcript",
            settings["transcript"],
        ]
    if settings["provider"] != "macos":
        raise ValueError("未知配音 provider")
    return [
        "say",
        "-v",
        settings["voice"],
        "-r",
        "185",
        "-f",
        str(text_file),
        "-o",
        str(output),
    ]


def spoken_text(text, settings):
    """Keep captions verbatim; only trusted, bounded controls reach the local TTS."""
    # Paper/model text must not inject synthesizer commands (including phoneme mode)
    text = text.replace("[[", "［［").replace("]]", "］］")
    if settings.get("provider") != "macos" or settings.get("prosody") != "classroom-v1":
        return text
    result = []
    for sentence in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        question = sentence.endswith(("？", "?"))
        contrast = sentence.startswith(
            ("但", "不过", "注意", "关键", "这里要", "需要区分")
        )
        # Subtle changes follow meaning, rather than oscillating on every comma
        delta = 1.2 if question else 0.6 if contrast else 0
        rate = 180 if question else 176 if contrast else 185
        result.append(f"[[rate {rate}]]")
        if delta:
            result.append(f"[[pbas +{delta}]][[pmod +1]]")
        result.append(sentence)
        if delta:
            result.append(f"[[pbas -{delta}]][[pmod -1]]")
        if question or contrast:
            result.append(f"[[slnc {140 if question else 80}]]")
    return "".join(result) + "[[rate 185]]"
