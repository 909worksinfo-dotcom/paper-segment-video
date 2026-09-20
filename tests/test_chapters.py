import copy
import json
import subprocess

import imageio_ffmpeg
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from test_studio import plan as sample_plan

from paper_video import chapters, flow, papers, planner, server, speech, store


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.setattr(chapters, "pause", lambda seconds, cancelled: None)
    store.init()
    return tmp_path


@pytest.fixture
def doc(database):
    with fitz.open() as pdf:
        for i in range(3):
            page = pdf.new_page()
            if i in (0, 2):
                page.insert_text(
                    (50, 60),
                    "1. Training" if i == 0 else "2. Evaluation",
                    fontname="hebo",
                    fontsize=14,
                )
            for y in range(100, 350, 25):
                page.insert_text(
                    (50, y),
                    "Attention stores keys and values. Cache reuse saves memory.",
                )
            page.insert_text((290, 800), str(i + 1))
        raw = pdf.tobytes()
    return papers.ingest(raw, "chapter-test.pdf")


def test_heading_scope_and_coverage(doc, tmp_path):
    title, fragments = chapters.extract(doc, 1)
    assert title == "1. Training"
    assert {f["page"] for f in fragments} == {1, 2}
    assert all(f["text"] not in {"1", "2", "2. Evaluation"} for f in fragments)
    selected = chapters.selection(fragments)
    assert set(selected["page_bboxes"]) == {"1", "2"}
    manifest = chapters.initialize(tmp_path, title, fragments)
    assert chapters.coverage(manifest)
    manifest["parts"][0]["begin"] = 1
    with pytest.raises(ValueError, match="遗漏"):
        chapters.coverage(manifest)
    with pytest.raises(ValueError, match="未唯一定位"):
        chapters.extract(doc, 3)


def test_adaptive_split_keeps_every_fragment(doc, tmp_path):
    title, fragments = chapters.extract(doc, 1)
    manifest = chapters.initialize(tmp_path, title, fragments)
    assert chapters.split_part(manifest, 0)
    assert chapters.coverage(manifest)
    assert len(manifest["parts"]) == 2
    assert manifest["parts"][0]["end"] == manifest["parts"][1]["begin"]
    assert manifest["superseded"][0]["depth"] == 0


def test_adaptive_split_does_not_choose_tiny_heading_chunk(tmp_path):
    fragments = [
        {"text": "x" * 90, "boundary": i in {0, 1}}
        for i in range(20)
    ]
    manifest = chapters.initialize(tmp_path, "Training", fragments)
    assert chapters.split_part(manifest, 0)
    assert chapters.coverage(manifest)
    lengths = [
        sum(len(f["text"]) + 1 for f in fragments[p["begin"] : p["end"]])
        for p in manifest["parts"]
    ]
    assert min(lengths) >= 350


def test_chunk_boundaries_have_no_loss_or_duplication():
    fragments = [{"text": "x" * 90, "boundary": i % 8 == 0} for i in range(400)]
    spans = chapters.ranges(fragments)
    assert [i for a, z in spans for i in range(a, z)] == list(range(400))
    assert all(a < z for a, z in spans)


def test_pending_boundary_keeps_cross_page_sentence_together(tmp_path):
    fragments = [
        {"text": "The model has twenty layers in the", "page": 1, "bbox": [0, 0, 10, 10]},
        {"text": "encoder and twenty layers in the", "page": 1, "bbox": [0, 20, 10, 30]},
        {"text": "decoder. The first two use a sliding window.", "page": 2, "bbox": [0, 0, 10, 10]},
        {"text": "The remaining layers use CSA2.", "page": 2, "bbox": [0, 20, 10, 30]},
    ]
    manifest = {
        "fragments": fragments,
        "source_digest": chapters.digest(fragments),
        "parts": [chapters.part(0, 2), chapters.part(2, 4)],
    }
    assert chapters.repair_pending_boundary(manifest, 0, tmp_path)
    assert [(part["begin"], part["end"]) for part in manifest["parts"]] == [
        (0, 3),
        (3, 4),
    ]
    assert chapters.coverage(manifest)
    assert not chapters.repair_pending_boundary(manifest, 0, tmp_path)


def test_pending_boundary_can_span_many_pdf_lines(tmp_path):
    fragments = [
        {"text": "sentence keeps going", "page": 1, "bbox": [0, i * 20, 10, i * 20 + 10]}
        for i in range(12)
    ]
    fragments[10]["text"] = "and ends here."
    fragments[11]["text"] = "The next topic starts."
    manifest = {
        "fragments": fragments,
        "source_digest": chapters.digest(fragments),
        "parts": [chapters.part(0, 2), chapters.part(2, 12)],
    }
    assert chapters.repair_pending_boundary(manifest, 0, tmp_path)
    assert manifest["parts"][0]["end"] == 11
    assert chapters.coverage(manifest)


def test_oversized_table_row_stays_intact():
    fragments = [
        {"text": "x" * 200, "page": 1, "bbox": [i * 50, 100, i * 50 + 40, 110]}
        for i in range(5)
    ]
    fragments.append({"text": "next " * 100, "page": 1, "bbox": [0, 150, 100, 160]})
    assert chapters.safe_cut(fragments, 0, 2) == 5
    assert chapters.ranges(fragments, target=450) == [(0, 5), (5, 6)]
    manifest = {
        "fragments": fragments[:5],
        "source_digest": chapters.digest(fragments[:5]),
        "parts": [chapters.part(0, 5)],
        "superseded": [],
    }
    assert not chapters.split_part(manifest, 0)
    assert chapters.coverage(manifest)


def test_retry_carries_latest_review_without_treating_it_as_source(tmp_path):
    first = tmp_path / "attempt-1"
    second = tmp_path / "attempt-2"
    first.mkdir()
    second.mkdir()
    (first / "review-2.json").write_text(
        json.dumps({"issues": ["旧问题"], "coverage_gaps": []}), encoding="utf-8"
    )
    (second / "review-1.json").write_text(
        json.dumps({"issues": ["箭头缺少数据流依据"], "coverage_gaps": []}),
        encoding="utf-8",
    )
    feedback = chapters.previous_review_feedback(tmp_path)
    assert "箭头缺少数据流依据" in feedback
    assert "旧问题" not in feedback
    assert "审核反馈而非论文原文" in feedback


def test_arrowless_static_is_chapter_only():
    scene = {
        "kind": "comparison",
        "narration": "比较三个模型的同一指标，箭头不表示任何执行顺序",
        "diagram_nodes": [],
        "flow": {
            "mode": "static",
            "nodes": [
                {"id": "a", "label": "甲", "row": 0, "column": 0},
                {"id": "b", "label": "乙", "row": 0, "column": 1},
            ],
            "edges": [],
            "steps": [
                {
                    "anchor": "比较三个模型",
                    "action": "比较",
                    "nodes": ["a", "b"],
                    "edges": [],
                    "input": "两列结果",
                    "output": "对照",
                    "condition": "同口径",
                    "evidence": "原文明确",
                }
            ],
        },
    }
    flow.validate_visuals(scene, allow_static=True)
    with pytest.raises(ValueError, match="流程图需要"):
        flow.validate_visuals(scene)


def test_short_chapter_part_does_not_need_repeated_intro_and_recap():
    plan = sample_plan()
    plan["scenes"] = plan["scenes"][:2]
    plan["scenes"][0]["kind"] = "concept"
    plan["scenes"][1]["kind"] = "concept"
    pages = [{"page": 1, "text": "Attention stores keys and values. Cache reuse saves memory."}]
    assert planner.validate_plan(plan, pages, chapter_mode=True)["covered_points"] == 2
    with pytest.raises(ValueError, match="知识点或场景数量"):
        planner.validate_plan(plan, pages)


def test_chapter_api_deduplicates_and_rejects_browser_generation(doc, monkeypatch):
    submitted = []
    monkeypatch.setattr(
        server.CHAPTER_POOL, "submit", lambda *args: submitted.append(args)
    )
    with TestClient(server.app, headers={"X-Paper-Client": "1"}) as client:
        body = {"document_id": doc["id"], "chapter": 1}
        assert (
            client.post(
                "/api/jobs/chapter", json=body, headers={"Origin": "http://testserver"}
            ).status_code
            == 403
        )
        first = client.post("/api/jobs/chapter", json=body)
        second = client.post("/api/jobs/chapter", json=body)
        assert first.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert len(submitted) == 1
        assert first.json()["speech"] == speech.profile()
        assert (
            client.get(f"/api/documents/{doc['id']}/chapters").json()[1]["chapter"]
            == "2"
        )


def make_job(doc, root):
    title, fragments = chapters.extract(doc, 1)
    job_id = "a" * 32
    directory = root / "jobs" / job_id
    chapters.initialize(directory, title, fragments)
    store.save_job(
        {
            "id": job_id,
            "kind": "chapter",
            "document_id": doc["id"],
            "title": title,
            "selection": chapters.selection(fragments),
            "created": 1,
            "state": "queued",
            "speech": speech.profile(),
            "cancel_requested": False,
        }
    )
    return job_id, directory


def fake_plan(
    selected,
    pages,
    images,
    directory,
    cancelled,
    progress,
    chapter_mode=False,
    initial_feedback="",
):
    assert chapter_mode
    plan = sample_plan()
    report = planner.validate_plan(plan, pages, chapter_mode=True)
    report["scientific_review"] = {"passed": True, "issues": [], "coverage_gaps": []}
    return plan, report


def fake_render(doc_id, selected, plan, directory, cancelled, progress, voice):
    (directory / "lesson.mp4").write_bytes(b"test-media")
    (directory / "poster.png").write_bytes(b"test-poster")
    (directory / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:00,800\n说明")
    return {"duration": 1, "timeline": [], "flow_steps": []}


def fake_assemble(root, manifest, voice, cancelled):
    assert chapters.coverage(manifest)
    assert all(p["state"] == "ready" for p in manifest["parts"])
    return (
        {"video": "lesson.mp4"},
        sample_plan(),
        {"scientific_review": {"passed": True}},
    )


def test_render_retry_reuses_reviewed_plan_and_completed_parts(
    doc, database, monkeypatch
):
    job_id, root = make_job(doc, database)
    calls = {"plan": 0, "render": 0}

    def plan(*args, **kwargs):
        calls["plan"] += 1
        return fake_plan(*args, **kwargs)

    def render(*args):
        calls["render"] += 1
        if calls["render"] == 1:
            raise ConnectionError("temporary speech outage")
        return fake_render(*args)

    monkeypatch.setattr(planner, "create_plan", plan)
    monkeypatch.setattr(chapters, "render", render)
    monkeypatch.setattr(chapters, "assemble", fake_assemble)
    chapters.run(job_id)
    assert store.job(job_id)["state"] == "ready"
    assert calls == {"plan": 1, "render": 2}
    store.update_job(job_id, state="failed")
    chapters.prepare_retry(job_id)
    chapters.run(job_id)
    assert store.job(job_id)["state"] == "ready"
    assert calls == {"plan": 1, "render": 2}
    # A modified script must be re-reviewed, even when the MP4 still matches its hash
    manifest = json.loads((root / "chapter.json").read_text())
    cached = root / "parts" / manifest["parts"][0]["id"] / "plan.json"
    value = json.loads(cached.read_text())
    value["title"] = "changed"
    chapters.write_json(cached, value)
    chapters.run(job_id)
    assert calls == {"plan": 2, "render": 3}


def test_review_failure_subdivides_without_silencing_failure(
    doc, database, monkeypatch
):
    job_id, root = make_job(doc, database)
    attempts = []

    def plan(selected, *args, **kwargs):
        attempts.append(len(selected["text"]))
        if len(selected["text"]) > 900:
            raise planner.PlanValidationError("needs a smaller source")
        return fake_plan(selected, *args, **kwargs)

    monkeypatch.setattr(planner, "create_plan", plan)
    monkeypatch.setattr(chapters, "render", fake_render)
    monkeypatch.setattr(chapters, "assemble", fake_assemble)
    chapters.run(job_id)
    manifest = json.loads((root / "chapter.json").read_text())
    assert store.job(job_id)["state"] == "ready"
    assert len(manifest["parts"]) == 2
    assert len(attempts) == 3
    assert chapters.coverage(manifest)


def test_retry_bound_and_cancel_propagation(doc, database, monkeypatch):
    job_id, root = make_job(doc, database)
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("offline")

    monkeypatch.setattr(planner, "create_plan", broken)
    chapters.run(job_id)
    assert store.job(job_id)["state"] == "failed"
    assert len(calls) == chapters.MAX_TRIES
    assert not (root / "lesson.mp4").exists()
    chapters.prepare_retry(job_id)
    store.update_job(job_id, cancel_requested=True)
    chapters.run(job_id)
    assert store.job(job_id)["state"] == "cancelled"
    assert len(calls) == chapters.MAX_TRIES


def test_review_failure_retries_leaf_before_failing_parent(doc, database, monkeypatch):
    job_id, root = make_job(doc, database)
    manifest = json.loads((root / "chapter.json").read_text())
    manifest["parts"][0]["depth"] = chapters.MAX_SPLIT_DEPTH
    chapters.write_json(root / "chapter.json", manifest)
    calls = []

    def review_failure(*args, **kwargs):
        calls.append(1)
        raise planner.PlanValidationError("review issue")

    monkeypatch.setattr(planner, "create_plan", review_failure)
    chapters.run(job_id)
    assert store.job(job_id)["state"] == "failed"
    assert len(calls) == chapters.MAX_TRIES
    saved = json.loads((root / "chapter.json").read_text())
    assert saved["parts"][0]["tries"] == chapters.MAX_TRIES
    assert not (root / "lesson.mp4").exists()


def test_restart_recovers_chapter_only(doc, database, monkeypatch):
    job_id, _root = make_job(doc, database)
    ordinary = copy.deepcopy(store.job(job_id))
    ordinary.update(id="b" * 32, kind="selection")
    store.save_job(ordinary)
    submitted = []
    monkeypatch.setattr(
        server.CHAPTER_POOL, "submit", lambda *args: submitted.append(args)
    )
    with TestClient(server.app):
        assert store.job(job_id)["state"] == "queued"
        assert store.job(job_id)["recoveries"] == 1
        assert store.job(ordinary["id"])["state"] == "failed"
    assert len(submitted) == 1


def test_real_media_merge_and_timestamp_offsets(doc, database):
    job_id, root = make_job(doc, database)
    manifest = json.loads((root / "chapter.json").read_text())
    assert chapters.split_part(manifest, 0)
    voice = store.job(job_id)["speech"]
    for i, leaf in enumerate(manifest["parts"]):
        directory = root / "parts" / leaf["id"]
        directory.mkdir(parents=True)
        selected = chapters.selection(
            manifest["fragments"][leaf["begin"] : leaf["end"]]
        )
        plan = sample_plan()
        report = {
            "scientific_review": {"passed": True, "issues": [], "coverage_gaps": []}
        }
        chapters.write_json(directory / "plan.json", plan)
        chapters.write_json(directory / "validation.json", report)
        chapters.write_json(
            directory / "approval.json",
            {
                "plan": chapters.digest(plan),
                "report": chapters.digest(report),
                "selection": chapters.digest(selected),
                "speech": chapters.digest(voice),
            },
        )
        duration = 1 + i
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=128x72:r=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=500:sample_rate=24000",
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(directory / "lesson.mp4"),
            ],
            check=True,
        )
        (directory / "poster.png").write_bytes(b"poster")
        (directory / "captions.srt").write_text(
            "1\n00:00:00,100 --> 00:00:00,800\n说明"
        )
        leaf.update(
            state="ready",
            media_digest=chapters.file_digest(directory / "lesson.mp4"),
            result={
                "duration": duration,
                "timeline": [
                    {
                        "index": 0,
                        "start": 0,
                        "end": duration,
                        "title": "说明",
                        "source_page": 1,
                    }
                ],
                "flow_steps": [{"scene": 0, "start": 0.2, "pause_at": 0.3}],
            },
        )
    result, plan, report = chapters.assemble(root, manifest, voice, lambda: False)
    assert result["duration"] == 3
    assert result["audio_verified"] and result["video_verified"]
    assert result["timeline"][1]["start"] == 1
    assert result["flow_steps"][1]["scene"] == 3
    assert result["flow_steps"][1]["start"] == 1.2
    assert chapters.parse_srt(root / "captions.srt")[1][0] == 1.1
    assert len({k["id"] for k in plan["knowledge_points"]}) == 4
    assert report["source_coverage"]["complete"]
    bad = root / "parts" / manifest["parts"][0]["id"] / "lesson.mp4"
    bad.write_bytes(b"truncated")
    with pytest.raises(ValueError, match="损坏"):
        chapters.assemble(root, manifest, voice, lambda: False)
