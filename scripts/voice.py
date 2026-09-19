#!/usr/bin/env python3
"""Run neural TTS in an isolated optional MLX environment, with local weights only."""

import argparse
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser()
    for name in ("text-file", "output", "model", "reference", "transcript"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    import mlx.core as mx
    import numpy as np
    from mlx_audio.audio_io import write
    from mlx_audio.tts.utils import load_model

    model = load_model(str(args.model))
    text = args.text_file.read_text(encoding="utf-8").strip()
    reference_text = args.transcript.read_text(encoding="utf-8").strip()
    if not text or not reference_text:
        raise ValueError("正文和参考录音转写都不能为空")
    # Small sentence groups prevent autoregressive repetition on long paragraphs
    groups, group = [], ""
    for sentence in re.split(r"(?<=[。！？；])", text):
        if group and len(group + sentence) > 160:
            groups.append(group)
            group = ""
        group += sentence
    if group:
        groups.append(group)
    audio = []
    mx.random.seed(42)
    for group in groups:
        for result in model.generate(
            text=group,
            ref_audio=str(args.reference),
            ref_text=reference_text,
            lang_code="Chinese",
            temperature=0.65,
            max_tokens=1800,
            split_pattern="",
            verbose=False,
        ):
            if result.token_count >= 1800:
                raise RuntimeError("神经配音达到长度上限，拒绝截断的音频")
            audio.append(np.asarray(result.audio, dtype=np.float32))
    if not audio:
        raise RuntimeError("参考音色合成没有返回音频")
    write(str(args.output), np.concatenate(audio), model.sample_rate)


if __name__ == "__main__":
    main()
