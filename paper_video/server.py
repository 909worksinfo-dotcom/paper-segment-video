"""Loopback-only HTTP service with one durable, cancellable video queue."""

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import papers, store, speech, text_selection, chapters
from .planner import Cancelled, create_plan
from .render import render

WEB = Path(__file__).resolve().parent.parent / "web"
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paper-video")
CHAPTER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paper-chapter")
CREATE_LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    store.init()
    for job in store.jobs():
        if job["state"] not in {"ready", "failed", "cancelled"}:
            if job.get("kind") == "chapter" and not job.get("cancel_requested") and job.get("recoveries", 0) < 3:
                store.update_job(job["id"], state="queued", message="正在从已保存的章节进度恢复", recoveries=job.get("recoveries", 0) + 1)
                CHAPTER_POOL.submit(chapters.run, job["id"])
                continue
            store.update_job(
                job["id"],
                state="failed",
                message="服务重启中断了任务，请在主会话要求重试",
                error="interrupted",
            )
    yield


app = FastAPI(title="Paper Segment Video", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
)


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "拒绝跨站写入"}, status_code=403)
        if request.headers.get("x-paper-client") != "1":
            return JSONResponse({"detail": "缺少客户端请求标记"}, status_code=403)
    # Older open tabs must also stop creating/retrying videos from the panel
    path = request.url.path
    generation = path in {"/api/jobs", "/api/jobs/paste", "/api/jobs/chapter"} or (path.startswith("/api/jobs/") and path.endswith("/retry"))
    if request.method == "POST" and generation and (request.headers.get("origin") or request.headers.get("sec-fetch-mode")):
        return JSONResponse({"detail": "请将选区引用到主会话，在对话框发送生成或重试指令"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.exception_handler(KeyError)
async def missing(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.get("/api/health")
def health():
    configured_api = bool(os.environ.get("PAPER_VIDEO_API_BASE"))
    return {
        "app": "paper-segment-video",
        "status": "ok",
        "version": "0.1.0",
        "api_revision": 3,
        "chapter_jobs": True,
        "pid": os.getpid(),
        "provider": "已配置的模型接口" if configured_api else "Codex 本地登录",
        "model_available": bool(os.environ.get("PAPER_VIDEO_MODEL"))
        if configured_api
        else bool(shutil.which("codex")),
        "speech_available": bool(shutil.which("say")),
        "active_jobs": sum(
            j["state"] not in {"ready", "failed", "cancelled"} for j in store.jobs()
        ),
    }


@app.get("/api/documents")
def documents():
    return [{k: v for k, v in doc.items() if k != "pages"} for doc in store.documents()]


@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    data = bytearray()
    try:
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > 100 * 1024 * 1024:
                raise HTTPException(413, "单个 PDF 不得超过 100 MB")
    finally:
        await file.close()
    return await asyncio.to_thread(
        papers.ingest, bytes(data), Path(file.filename or "paper.pdf").name
    )


@app.get("/api/documents/{doc_id}")
def document(doc_id: str):
    return store.document(doc_id)


@app.get("/api/documents/{doc_id}/pages/{number}.png")
def page_image(doc_id: str, number: int):
    doc = store.document(doc_id)
    if not 1 <= number <= doc["page_count"]:
        raise HTTPException(404, "页码不存在")
    directory = store.ROOT / "papers" / doc_id
    output = directory / f"page-{number}.png"
    if not output.exists():
        # Unique temporary files avoid simultaneous thumbnail requests corrupting PNGs
        tmp = directory / f"{uuid.uuid4().hex}.png"
        papers.page_image(doc_id, number, tmp)
        tmp.replace(output)
    return FileResponse(output, media_type="image/png")


class TextEndpoint(BaseModel):
    page: int = Field(ge=1, le=300, strict=True)
    line: int = Field(ge=0, strict=True)
    offset: int = Field(ge=0, strict=True)


class TextRange(BaseModel):
    start: TextEndpoint
    end: TextEndpoint


class SelectionRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=64)
    page: int = Field(ge=1, le=300)
    block_id: str | None = Field(default=None, max_length=100)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    text_range: TextRange | None = None


class PasteRequest(BaseModel):
    text: str = Field(min_length=20, max_length=12000)
    document_id: str | None = Field(default=None, max_length=64)


class ChapterRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=64)
    chapter: int = Field(ge=1, le=999, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=120)


def worker(job_id):
    def cancelled():
        return store.job(job_id).get("cancel_requested", False)

    def progress(state, message):
        if cancelled():
            raise Cancelled()
        store.update_job(job_id, state=state, message=message)

    job = store.job(job_id)
    directory = store.ROOT / "jobs" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if cancelled():
            raise Cancelled()
        doc = store.document(job["document_id"])
        selected = job["selection"]
        pages = papers.context(doc, selected)
        images = []
        # The selection page and both neighbors are visible to the model for formulas/figures
        for number in text_selection.image_pages(selected, doc["page_count"]):
            image = directory / f"source-page-{number}.png"
            papers.page_image(doc["id"], number, image)
            images.append(image)
        (directory / "context.json").write_text(
            json.dumps(
                {"selection": selected, "pages": pages}, ensure_ascii=False, indent=2
            )
        )
        if (directory / "plan.json").exists():
            # Restart of rendering can reuse an already-reviewed plan
            from .planner import validate_plan

            plan = json.loads((directory / "plan.json").read_text())
            report = validate_plan(plan, pages)
            review = json.loads((directory / "validation.json").read_text())
            if not review.get("scientific_review", {}).get("passed"):
                raise ValueError("缓存脚本缺少科学审核通过记录")
            report.update(review)
        else:
            plan, report = create_plan(
                selected, pages, images, directory, cancelled, progress
            )
        store.update_job(job_id, title=plan["title"], plan=plan, validation=report)
        result = render(
            doc["id"], selected, plan, directory, cancelled, progress, job.get("speech")
        )
        if cancelled():
            raise Cancelled()
        store.update_job(
            job_id,
            state="ready",
            message="视频已就绪 · 含中文配音、动态批注和字幕",
            result=result,
            error=None,
        )
    except Cancelled:
        store.update_job(job_id, state="cancelled", message="任务已取消")
    except Exception as exc:
        logging.exception("Job %s failed", job_id)
        store.update_job(
            job_id, state="failed", message="生成失败，可重试", error=str(exc)[:700]
        )


def resolve_selection(doc, body):
    if body.text_range:
        if body.block_id or body.bbox:
            raise ValueError("字符选区不能与段落或框选区域混用")
        return text_selection.resolve(doc, body.text_range.model_dump())
    return papers.selection(doc, body.page, body.block_id, body.bbox)


@app.get("/api/documents/{doc_id}/text-layer")
def text_layer(doc_id: str):
    return text_selection.layout(doc_id)


@app.get("/api/documents/{doc_id}/chapters")
def chapter_headings(doc_id: str):
    return chapters.headings(store.document(doc_id))


@app.post("/api/selections")
def preview_selection(body: SelectionRequest):
    return resolve_selection(store.document(body.document_id), body)


@app.post("/api/quotes", status_code=201)
def create_quote(body: SelectionRequest):
    doc = store.document(body.document_id)
    selected = resolve_selection(doc, body)
    value = {"id": uuid.uuid4().hex, "document_id": doc["id"], "title": doc["title"],
             "selection": selected, "request": body.model_dump(), "created": time.time()}
    store.save_quote(value)
    return value


@app.get("/api/quotes/{quote_id}")
def get_quote(quote_id: str):
    return store.quote(quote_id)


@app.post("/api/jobs", status_code=202)
def create_job(body: SelectionRequest):
    doc = store.document(body.document_id)
    selected = resolve_selection(doc, body)
    return enqueue(doc, selected)


@app.post("/api/jobs/paste", status_code=202)
def paste_job(body: PasteRequest):
    doc, selected = papers.pasted_selection(body.text, body.document_id)
    return enqueue(doc, selected)


@app.post("/api/jobs/chapter", status_code=202)
def chapter_job(body: ChapterRequest):
    doc = store.document(body.document_id)
    title, fragments = chapters.extract(doc, body.chapter)
    title = body.title or title
    selected, voice = chapters.selection(fragments), speech.profile()
    fingerprint = chapters.digest({"document": doc["id"], "fragments": fragments, "speech": voice, "title": title})
    with CREATE_LOCK:
        existing = store.jobs()
        for value in existing:
            if value.get("kind") == "chapter" and value.get("fingerprint") == fingerprint:
                return value
        if sum(j["state"] not in {"ready", "failed", "cancelled"} for j in existing) >= 6:
            raise HTTPException(429, "本地队列已满（最多 6 个任务），请等待或取消已有任务")
        job_id = uuid.uuid4().hex
        manifest = chapters.initialize(store.ROOT / "jobs" / job_id, title, fragments)
        value = {"id": job_id, "kind": "chapter", "chapter": body.chapter,
                 "document_id": doc["id"], "selection": selected, "fingerprint": fingerprint,
                 "created": time.time(), "state": "queued", "message": f"章节已分为 {len(manifest['parts'])} 段，正在排队生成",
                 "title": title, "panel_path": "/?panel=1&job=" + job_id,
                 "speech_speed": voice["speed"], "speech_label": voice["label"], "speech": voice,
                 "chapter_progress": {"ready": 0, "total": len(manifest["parts"])},
                 "cancel_requested": False, "recoveries": 0}
        store.save_job(value)
        CHAPTER_POOL.submit(chapters.run, job_id)
    return value


def enqueue(doc, selected):
    voice = speech.profile()
    with CREATE_LOCK:
        active = [
            j
            for j in store.jobs()
            if j["state"] not in {"ready", "failed", "cancelled"}
        ]
        for job in active:
            if (
                job["document_id"] == doc["id"]
                and job["selection"] == selected
                and job.get("speech") == voice
            ):
                return job
        if len(active) >= 6:
            raise HTTPException(
                429, "本地队列已满（最多 6 个任务），请等待或取消已有任务"
            )
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "document_id": doc["id"],
            "selection": selected,
            "created": time.time(),
            "state": "queued",
            "message": "已加入本地生成队列",
            "title": "主会话粘贴原文"
            if selected.get("input_mode") == "chat"
            else "第 " + str(selected["page"]) + " 页选段",
            "panel_path": "/?panel=1&job=" + job_id,
            "speech_speed": voice["speed"],
            "speech_label": voice["label"],
            "speech": voice,
            "cancel_requested": False,
        }
        store.save_job(job)
        POOL.submit(worker, job_id)
    return job


@app.get("/api/jobs")
def jobs():
    return store.jobs()


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    return store.job(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    job = store.job(job_id)
    if job["state"] in {"ready", "failed", "cancelled"}:
        return job
    return store.update_job(job_id, cancel_requested=True, message="正在取消任务")


@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry(job_id: str):
    with CREATE_LOCK:
        job = store.job(job_id)
        if job["state"] not in {"failed", "cancelled"}:
            raise HTTPException(409, "只有失败或取消的任务可以重试")
        if (
            sum(
                j["state"] not in {"ready", "failed", "cancelled"} for j in store.jobs()
            )
            >= 6
        ):
            raise HTTPException(429, "队列已满，请稍后重试")
        if job.get("kind") == "chapter":
            chapters.prepare_retry(job_id)
        result = store.update_job(
            job_id,
            state="queued",
            message="已重新加入队列",
            cancel_requested=False,
            error=None,
        )
        if job.get("kind") == "chapter":
            result = store.update_job(job_id, recoveries=0)
            CHAPTER_POOL.submit(chapters.run, job_id)
        else:
            POOL.submit(worker, job_id)
        return result


@app.get("/api/jobs/{job_id}/files/{name}")
def artifact(job_id: str, name: str):
    job = store.job(job_id)
    allowed = {
        "lesson.mp4": "video/mp4",
        "poster.png": "image/png",
        "captions.vtt": "text/vtt",
        "captions.srt": "application/x-subrip",
        "plan.json": "application/json",
        "validation.json": "application/json",
    }
    if name not in allowed:
        raise HTTPException(404)
    if job["state"] != "ready":
        raise HTTPException(409, "视频尚未完成")
    file = store.ROOT / "jobs" / job_id / name
    if not file.is_file():
        raise HTTPException(404)
    return FileResponse(file, media_type=allowed[name])


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
