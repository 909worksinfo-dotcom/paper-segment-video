#!/usr/bin/env python3
"""Explicitly selected Edge neural voice; never silently substitute another voice."""

import argparse
import asyncio
import json
from pathlib import Path

import edge_tts


async def synthesize(text, voice, rate, output, timing):
    if voice not in {"zh-CN-YunxiNeural", "zh-CN-YunyangNeural", "zh-CN-YunjianNeural"}:
        raise ValueError("请选择已验证的中文神经男声")
    partial = output.with_suffix(output.suffix + ".part")
    for attempt in range(3):
        boundaries = []
        try:
            async with asyncio.timeout(65):
                with partial.open("wb") as audio:
                    stream = edge_tts.Communicate(
                        text,
                        voice,
                        rate=rate,
                        boundary="WordBoundary",
                        connect_timeout=10,
                        receive_timeout=30,
                    )
                    async for chunk in stream.stream():
                        if chunk["type"] == "audio":
                            audio.write(chunk["data"])
                        elif chunk["type"] == "WordBoundary":
                            boundaries.append(
                                {
                                    "start": chunk["offset"] / 10_000_000,
                                    "duration": chunk["duration"] / 10_000_000,
                                    "text": chunk["text"],
                                }
                            )
            if partial.stat().st_size < 1024:
                raise RuntimeError("神经语音返回空音频")
            partial.replace(output)
            timing.write_text(
                json.dumps(boundaries, ensure_ascii=False), encoding="utf-8"
            )
            return
        except asyncio.CancelledError:
            partial.unlink(missing_ok=True)
            raise
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--rate", default="+0%")
    args = parser.parse_args()
    asyncio.run(
        synthesize(
            args.text_file.read_text(encoding="utf-8"),
            args.voice,
            args.rate,
            args.output,
            args.output.with_suffix(".timing.json"),
        )
    )


if __name__ == "__main__":
    main()
