import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from paper_video import papers, planner, store
from paper_video.server import app


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    store.init()
    return tmp_path


def pdf_bytes(text="Attention stores keys and values. Cache reuse saves memory."):
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 100), text)
        return pdf.tobytes()


@pytest.fixture
def doc(database):
    return papers.ingest(pdf_bytes(), "test.pdf")


@pytest.fixture
def client(database):
    with TestClient(app, headers={"X-Paper-Client": "1"}) as client:
        yield client


def plan():
    p = {
        "title": "缓存复用",
        "takeaway": "复用缓存可以减少重复存储",
        "knowledge_points": [
            {
                "id": "k1",
                "concept": "缓存",
                "explanation": "存储键和值",
                "type": "source",
                "evidence": [{"page": 1, "quote": "Attention stores keys and values."}],
            },
            {
                "id": "k2",
                "concept": "复用",
                "explanation": "减少重复存储",
                "type": "source",
                "evidence": [{"page": 1, "quote": "Cache reuse saves memory."}],
            },
        ],
        "scenes": [],
        "limitations": ["不能推断延迟变化"],
        "check_question": "节省什么？",
        "check_answer": "存储",
    }
    for kind in ["motivation", "limits", "recap"]:
        p["scenes"].append(
            {
                "title": "理解缓存",
                "narration": "这里介绍键和值的缓存，以及复用缓存为什么可以减少重复存储。",
                "bullets": ["缓存保存键和值"],
                "source_page": 1,
                "highlight_quote": "keys and values",
                "knowledge_ids": ["k1", "k2"],
                "kind": kind,
                "formula": "",
                "diagram_nodes": [],
            }
        )
    return p


CONTEXT = [
    {"page": 1, "text": "Attention stores keys and values. Cache reuse saves memory."}
]


def test_import_and_coordinates(doc):
    assert doc["page_count"] == 1
    block = doc["pages"][0]["blocks"][0]
    assert block["bbox"][2] > block["bbox"][0]
    assert papers.selection(doc, 1, block["id"])["text"] == block["text"]


def test_repeat_import_is_content_addressed(doc):
    raw = (papers.path(doc["id"]).parent / "uploaded.pdf").read_bytes()
    assert papers.ingest(raw, "renamed.pdf")["id"] == doc["id"]
    assert len(store.documents()) == 1


def test_rotated_pdf_coordinates(database):
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 100), "Rotated equation and text")
        page.set_rotation(90)
        data = pdf.tobytes()
    doc = papers.ingest(data, "rotated.pdf")
    page = doc["pages"][0]
    assert page["width"] > page["height"]
    for b in page["blocks"]:
        x0, y0, x1, y1 = b["bbox"]
        assert 0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]
        assert (
            papers.selection(doc, 1, bbox=b["bbox"])["text"].strip()
            == b["text"].strip()
        )


def test_encrypted_pdf_rejected(database):
    with fitz.open() as pdf:
        pdf.new_page()
        raw = pdf.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret")
    with pytest.raises(ValueError, match="密码"):
        papers.ingest(raw, "locked.pdf")


@pytest.mark.parametrize("raw", [b"", b"not a pdf", b"%PDF-this-is-broken"])
def test_invalid_pdf(database, raw):
    with pytest.raises(ValueError):
        papers.ingest(raw, "bad.pdf")


@pytest.mark.parametrize(
    "page,bid,bbox",
    [
        (0, None, None),
        (2, None, None),
        (1, "p2-b0", None),
        (1, None, [-1, 0, 40, 40]),
        (1, None, [0, 0, 0, 40]),
        (1, None, [0, 0, 9999, 40]),
        (1, None, [0, 0, float("nan"), 40]),
    ],
)
def test_selection_boundaries(doc, page, bid, bbox):
    with pytest.raises(ValueError):
        papers.selection(doc, page, bid, bbox)


def test_valid_region(doc):
    selection = papers.selection(doc, 1, bbox=[40, 80, 500, 120])
    assert "Attention" in selection["text"]


def test_scan_page_and_image(database, tmp_path):
    doc = papers.ingest(pdf_bytes(""), "scan.pdf")
    assert doc["text_pages"] == 0
    assert papers.selection(doc, 1, bbox=[20, 20, 100, 100])["text"] == ""
    out = tmp_path / "page.png"
    papers.page_image(doc["id"], 1, out)
    assert out.read_bytes().startswith(b"\x89PNG")


def test_grounded_plan():
    assert planner.validate_plan(plan(), CONTEXT)["covered_points"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["knowledge_points"][0]["evidence"][0].update(
            quote="invented quotation"
        ),
        lambda p: p["knowledge_points"][0].update(evidence=[]),
        lambda p: p["knowledge_points"][1].update(id="k1"),
        lambda p: p["scenes"][0].update(source_page=2),
        lambda p: p["scenes"][0].update(highlight_quote="not present in paper"),
        lambda p: p["scenes"][0].update(knowledge_ids=["unknown"]),
        lambda p: p["scenes"][0].update(narration="too short"),
        lambda p: p["scenes"][0].update(kind="concept"),
    ],
)
def test_reject_ungrounded_or_incomplete_plan(mutation):
    p = plan()
    mutation(p)
    with pytest.raises(ValueError):
        planner.validate_plan(p, CONTEXT)


def test_uncovered_point():
    p = plan()
    for s in p["scenes"]:
        s["knowledge_ids"] = ["k1"]
    with pytest.raises(ValueError, match="未讲解"):
        planner.validate_plan(p, CONTEXT)


def test_normalization():
    assert papers.normal("𝐿 × 𝑛\nwin") == papers.normal("L × nwin")
    assert papers.normal("archi-\ntecture") == papers.normal("architecture")
    assert papers.normal("long-context") != papers.normal("longcontext")


def test_csrf_and_upload(client):
    assert (
        client.post(
            "/api/documents",
            headers={"Origin": "https://malicious.example"},
            files={"file": ("x.pdf", pdf_bytes())},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/documents",
            headers={"X-Paper-Client": ""},
            files={"file": ("x.pdf", pdf_bytes())},
        ).status_code
        == 403
    )
    response = client.post("/api/documents", files={"file": ("x.pdf", pdf_bytes())})
    assert response.status_code == 200
    doc = response.json()
    assert (
        client.get("/api/documents/" + doc["id"] + "/pages/1.png").headers[
            "content-type"
        ]
        == "image/png"
    )
    assert client.get("/api/documents/" + doc["id"] + "/pages/0.png").status_code == 404
    assert client.get("/api/documents/not-a-document").status_code == 404


def test_queue_dedup_cancel_retry(client, monkeypatch):
    from paper_video import server

    submitted = []
    monkeypatch.setattr(server.POOL, "submit", lambda *a: submitted.append(a))
    doc = papers.ingest(pdf_bytes(), "test.pdf")
    body = {
        "document_id": doc["id"],
        "page": 1,
        "block_id": doc["pages"][0]["blocks"][0]["id"],
    }
    a = client.post("/api/jobs", json=body).json()
    b = client.post("/api/jobs", json=body).json()
    assert a["id"] == b["id"] and len(submitted) == 1
    assert client.post("/api/jobs/" + a["id"] + "/retry").status_code == 409
    assert client.post("/api/jobs/" + a["id"] + "/cancel").json()["cancel_requested"]
    server.worker(a["id"])
    assert store.job(a["id"])["state"] == "cancelled"
    assert client.post("/api/jobs/" + a["id"] + "/retry").status_code == 202
    assert len(submitted) == 2
    assert client.get("/api/jobs/" + a["id"] + "/files/lesson.mp4").status_code == 409
    assert client.get("/api/jobs/" + a["id"] + "/files/context.json").status_code == 404


def test_queue_limit(client, monkeypatch):
    from paper_video import server

    monkeypatch.setattr(server.POOL, "submit", lambda *a: None)
    doc = papers.ingest(pdf_bytes(), "test.pdf")
    for i in range(6):
        assert (
            client.post(
                "/api/jobs",
                json={
                    "document_id": doc["id"],
                    "page": 1,
                    "bbox": [10 + i * 10, 10, 30 + i * 10, 30],
                },
            ).status_code
            == 202
        )
    assert (
        client.post(
            "/api/jobs",
            json={"document_id": doc["id"], "page": 1, "bbox": [200, 10, 220, 30]},
        ).status_code
        == 429
    )


def test_subtitle_has_every_character():
    from paper_video.render import subtitle_chunks

    text = "这是较长的公式讲解。先说明符号，再介绍每个计算步骤；最后回到论文证据。"
    assert "".join(subtitle_chunks(text)) == text


def test_math_glyph_fallback_preserves_indices():
    from paper_video.render import readable_formula

    assert readable_formula("Cₗ = H₍L/2₎Wₗᴷⱽ") == "C[l] = H[L/2]W[l]^(KV)"
    assert readable_formula("[1,2]ᵀ") == "[1,2]^(T)"


def test_paste_matches_imported_pdf(doc):
    text = "Attention stores keys and values.\nCache reuse saves memory."
    matched, selected = papers.pasted_selection(text)
    assert matched["id"] == doc["id"]
    assert selected["text"] == text
    assert selected["input_mode"] == "chat"
    assert selected["source_pages"] == [1]


def test_paste_unmatched_stays_self_contained(database):
    text = "注意力机制使用查询、键和值计算输出，这里只提供这一段，没有实验数据。"
    doc, selected = papers.pasted_selection(text)
    assert doc["source_kind"] == "pasted"
    assert doc["original_text"] == text
    assert "不是论文页码" in selected["context_note"]
    assert papers.normal(text) in papers.normal(doc["pages"][0]["text"])
    assert papers.quote_rects(doc["id"], 1, "注意力机制")
    again, _ = papers.pasted_selection(text)
    assert again["id"] == doc["id"]


def test_paste_multi_page(database):
    text = "这是很长的论文原文，包含注意力机制和缓存复用的不同原理。" * 100
    doc, selected = papers.pasted_selection(text)
    assert doc["page_count"] > 1
    assert len(papers.context(doc, selected)) == doc["page_count"]
    assert papers.normal(text) == papers.normal(
        "".join(p["text"] for p in doc["pages"])
    )


@pytest.mark.parametrize("text", ["short", " " * 30, "x" * 12001])
def test_paste_invalid(database, text):
    with pytest.raises(ValueError):
        papers.pasted_selection(text)


def test_paste_endpoint_and_idempotency(client, doc, monkeypatch):
    from paper_video import server

    monkeypatch.setattr(server.POOL, "submit", lambda *args: None)
    response = client.post("/api/jobs/paste", json={"text": doc["pages"][0]["text"]})
    assert response.status_code == 202
    job = response.json()
    assert job["panel_path"] == "/?panel=1&job=" + job["id"]
    assert job["speech_speed"] == 1.0
    assert job["document_id"] == doc["id"]
    again = client.post("/api/jobs/paste", json={"text": doc["pages"][0]["text"]})
    assert again.json()["id"] == job["id"]
    assert client.get("/api/jobs/" + job["id"]).status_code == 200
    assert client.post("/api/jobs/paste", json={"text": " " * 40}).status_code == 422
    assert client.post("/api/jobs/paste", json={"text": "z" * 12001}).status_code == 422


def test_reference_speech_never_silently_falls_back():
    from paper_video import speech

    with pytest.raises(ValueError):
        speech.command({"provider": "unknown"}, "input.txt", "out.wav")


def test_ambiguous_paste_does_not_choose_a_paper(doc):
    text = doc["pages"][0]["text"].strip()
    with fitz.open() as pdf:
        p = pdf.new_page()
        p.insert_text((50, 100), text)
        p.insert_text((50, 200), text)
        papers.ingest(pdf.tobytes(), "ambiguous.pdf")
    resolved, selected = papers.pasted_selection(text)
    assert resolved["source_kind"] == "pasted"
    resolved, selected = papers.pasted_selection(text, doc["id"])
    assert resolved["id"] == doc["id"]


def test_paste_matches_across_pages_and_keeps_both(database):
    first = "An attention module stores keys and values."
    second = "The decoder reuses the global cache from the encoder."
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 100), first)
        pdf.new_page().insert_text((50, 100), second)
        source = papers.ingest(pdf.tobytes(), "cross-page.pdf")
    resolved, selected = papers.pasted_selection(first + "\n" + second)
    assert resolved["id"] == source["id"]
    assert selected["source_pages"] == [1, 2]
    assert {p["page"] for p in papers.context(resolved, selected)} == {1, 2}
