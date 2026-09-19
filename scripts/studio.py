#!/usr/bin/env python3
"""Portable plugin entry point; all mutable runtime data lives outside the plugin."""

import argparse
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

PLUGIN = Path(__file__).resolve().parent.parent
RUNTIME = Path.home() / ".local/share/paper-segment-video"
PYTHON = RUNTIME / "venv/bin/python"


def default_port():
    """Follow the active local studio during a rollout; explicit --port wins."""
    try:
        value = int((RUNTIME / "active-port").read_text().strip())
        return value if 1024 <= value <= 65535 else 8766
    except (OSError, ValueError):
        return 8766


def setup():
    if PYTHON.exists():
        check = subprocess.run(
            [
                str(PYTHON),
                "-c",
                "import fastapi, pymupdf, imageio_ffmpeg, jsonschema, httpx, PIL, edge_tts",
            ],
            capture_output=True,
        )
        if check.returncode == 0:
            return
    RUNTIME.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "venv", str(RUNTIME / "venv")], check=True)
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(PYTHON),
                "-r",
                str(PLUGIN / "requirements.txt"),
            ],
            check=True,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "venv", str(RUNTIME / "venv")], check=True
        )
        subprocess.run(
            [
                str(PYTHON),
                "-m",
                "pip",
                "install",
                "-r",
                str(PLUGIN / "requirements.txt"),
            ],
            check=True,
        )


def request(port, path, body=None):
    headers = {"X-Paper-Client": "1", "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(
        description="Start or control the local paper-to-video studio"
    )
    parser.add_argument(
        "action",
        choices=["start", "status", "documents", "jobs", "explain", "paste", "job", "quote", "retry"],
    )
    parser.add_argument("--port", type=int, default=default_port())
    parser.add_argument("--paper", type=Path)
    parser.add_argument("--document")
    parser.add_argument("--page", type=int)
    parser.add_argument("--block")
    parser.add_argument("--bbox", type=float, nargs=4)
    parser.add_argument("--job-id")
    parser.add_argument("--quote-id")
    parser.add_argument(
        "--text-file", type=Path, help="UTF-8 原文文件；使用 - 从标准输入读取"
    )
    args = parser.parse_args()
    if args.quote_id and not re.fullmatch(r"[0-9a-f]{32}", args.quote_id):
        parser.error("引用 ID 必须是 32 位小写十六进制字符")
    if args.action == "start":
        setup()
        if args.paper:
            subprocess.run(
                [
                    str(PYTHON),
                    "-m",
                    "paper_video",
                    "--paper",
                    str(args.paper.resolve()),
                    "--import-only",
                ],
                cwd=PLUGIN,
                check=True,
            )
        try:
            status = request(args.port, "/health")
            if status.get("app") != "paper-segment-video":
                raise SystemExit("端口已被其他服务占用，请指定 --port")
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            with open(RUNTIME / "server.log", "ab") as log:
                process = subprocess.Popen(
                    [str(PYTHON), "-m", "paper_video", "--port", str(args.port)],
                    cwd=PLUGIN,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            (RUNTIME / "server.pid").write_text(str(process.pid))
            for _ in range(100):
                try:
                    status = request(args.port, "/health")
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError):
                    if process.poll() is not None:
                        raise SystemExit(
                            f"服务启动失败，请查看 {RUNTIME / 'server.log'}"
                        )
                    time.sleep(0.2)
            else:
                raise SystemExit("服务启动超时，请查看本地 server.log")
        print(
            json.dumps(
                {"url": f"http://127.0.0.1:{args.port}", **status}, ensure_ascii=False
            )
        )
    elif args.action == "status":
        print(json.dumps(request(args.port, "/health"), ensure_ascii=False))
    elif args.action in ["documents", "jobs"]:
        print(
            json.dumps(
                request(args.port, "/" + args.action), ensure_ascii=False, indent=2
            )
        )
    elif args.action == "job":
        if not args.job_id:
            parser.error("job 需要 --job-id")
        print(
            json.dumps(
                request(args.port, "/jobs/" + args.job_id), ensure_ascii=False, indent=2
            )
        )
    elif args.action == "paste":
        if not args.text_file:
            parser.error("paste 需要 --text-file <UTF-8 文件>，或 --text-file -")
        text = (
            sys.stdin.read()
            if str(args.text_file) == "-"
            else args.text_file.read_text(encoding="utf-8")
        )
        job = request(
            args.port, "/jobs/paste", {"text": text, "document_id": args.document}
        )
        job["panel_url"] = f"http://127.0.0.1:{args.port}" + job["panel_path"]
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif args.action == "retry":
        if not args.job_id:
            parser.error("retry 需要 --job-id")
        job = request(args.port, "/jobs/" + args.job_id + "/retry", {})
        job["panel_url"] = f"http://127.0.0.1:{args.port}" + job["panel_path"]
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif args.action == "quote":
        if not args.quote_id:
            parser.error("quote 需要 --quote-id")
        print(json.dumps(request(args.port, "/quotes/" + args.quote_id), ensure_ascii=False, indent=2))
    elif args.action == "explain" and args.quote_id:
        quoted = request(args.port, "/quotes/" + args.quote_id)
        job = request(args.port, "/jobs", quoted["request"])
        job["panel_url"] = f"http://127.0.0.1:{args.port}" + job["panel_path"]
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif args.action == "explain":
        if not args.document or not args.page:
            parser.error("explain 需要 --document 和 --page")
        print(
            json.dumps(
                request(
                    args.port,
                    "/jobs",
                    {
                        "document_id": args.document,
                        "page": args.page,
                        "block_id": args.block,
                        "bbox": args.bbox,
                    },
                ),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
