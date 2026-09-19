import json
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from paper_video import papers, store, text_selection
from paper_video.server import app


@pytest.fixture
def doc(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    store.init()
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 80), "Alpha beta gamma.\nEnd of first page.")
        pdf.new_page().insert_text((50, 80), "Start of second page.\nDelta epsilon.")
        return papers.ingest(pdf.tobytes(), "range.pdf")


def endpoint(page, line, offset):
    return {"page": page, "line": line, "offset": offset}


def test_single_character_not_whole_paragraph(doc):
    chosen = text_selection.resolve(
        doc, {"start": endpoint(1, 0, 6), "end": endpoint(1, 0, 7)}
    )
    assert chosen["text"] == "b"
    assert chosen["bbox"][2] - chosen["bbox"][0] < 10
    assert chosen["source_pages"] == [1]


def test_partial_words_across_pages_and_reverse_drag(doc):
    selected = {"start": endpoint(1, 1, 7), "end": endpoint(2, 0, 5)}
    result = text_selection.resolve(doc, selected)
    assert result["text"] == "first page.\nStart"
    assert result["source_pages"] == [1, 2]
    assert set(result["page_bboxes"]) == {"1", "2"}
    assert (
        text_selection.resolve(
            doc, {"start": selected["end"], "end": selected["start"]}
        )
        == result
    )
    assert {p["page"] for p in papers.context(doc, result)} >= {1, 2}
    assert text_selection.image_pages(result, 2) == [1, 2]


@pytest.mark.parametrize(
    "start,end",
    [
        (endpoint(0, 0, 0), endpoint(1, 0, 1)),
        (endpoint(1, 99, 0), endpoint(2, 0, 1)),
        (endpoint(1, 0, 900), endpoint(2, 0, 1)),
        (endpoint(1, 0, 0), endpoint(1, 0, 0)),
        (endpoint(1, 0, 5), endpoint(1, 0, 6)),
        (endpoint(1, 0, True), endpoint(1, 0, 3)),
    ],
)
def test_invalid_empty_and_whitespace_ranges_rejected(doc, start, end):
    with pytest.raises(ValueError):
        text_selection.resolve(doc, {"start": start, "end": end})


def test_utf16_does_not_split_math_or_supplementary_characters():
    assert text_selection.utf16_index("A𝐿中", 3) == 2
    assert text_selection.utf16_index("A𝐿中", 4) == 3
    with pytest.raises(ValueError):
        text_selection.utf16_index("A𝐿中", 2)


def test_layout_preserves_canonical_line_offsets(doc):
    a = text_selection.layout(doc["id"])
    assert a == text_selection.layout(doc["id"])
    assert a[0]["lines"][0]["text"] == "Alpha beta gamma."
    assert "chars" not in a[0]["lines"][0]


def test_preview_and_job_use_identical_verified_range(doc, monkeypatch):
    from paper_video import server

    queued = []
    monkeypatch.setattr(server.POOL, "submit", lambda *args: queued.append(args))
    body = {
        "document_id": doc["id"],
        "page": 1,
        "text_range": {"start": endpoint(1, 1, 7), "end": endpoint(2, 0, 5)},
    }
    with TestClient(app, headers={"X-Paper-Client": "1"}) as client:
        assert (
            client.get("/api/documents/" + doc["id"] + "/text-layer").status_code == 200
        )
        preview = client.post("/api/selections", json=body)
        assert preview.status_code == 200
        response = client.post("/api/jobs", json=body)
        assert response.status_code == 202
        assert response.json()["selection"] == preview.json()
        assert len(queued) == 1
        response = client.post("/api/selections", json={**body, "block_id": "p1-b0"})
        assert response.status_code == 422
        bad = json.loads(json.dumps(body))
        bad["text_range"]["start"]["offset"] = 1.5
        assert client.post("/api/jobs", json=bad).status_code == 422


def test_quote_preserves_cross_page_selection_without_enqueue(doc, monkeypatch):
    from paper_video import server
    queued = []
    monkeypatch.setattr(server.POOL, "submit", lambda *args: queued.append(args))
    body = {"document_id": doc["id"], "page": 1,
            "text_range": {"start": endpoint(1, 1, 7), "end": endpoint(2, 0, 5)}}
    with TestClient(app, headers={"X-Paper-Client": "1"}) as client:
        response = client.post("/api/quotes", json=body, headers={"Origin": "http://testserver"})
        assert response.status_code == 201
        quote = response.json()
        assert quote["selection"]["text"] == "first page.\nStart"
        assert quote["selection"]["source_pages"] == [1, 2]
        assert not queued and not store.jobs()
        saved = client.get("/api/quotes/" + quote["id"]).json()
        assert saved == quote
        job = client.post("/api/jobs", json=saved["request"])
        assert job.status_code == 202
        assert job.json()["selection"] == quote["selection"]
        assert job.json()["speech_speed"] == 1.0
        assert len(queued) == 1


@pytest.mark.parametrize("path", ["/api/jobs", "/api/jobs/paste", "/api/jobs/old-job/retry"])
@pytest.mark.parametrize("headers", [{"Origin": "http://testserver"}, {"Sec-Fetch-Mode": "same-origin"}])
def test_panel_cannot_generate_even_from_old_tabs(doc, monkeypatch, path, headers):
    from paper_video import server
    queued = []
    monkeypatch.setattr(server.POOL, "submit", lambda *args: queued.append(args))
    with TestClient(app, headers={"X-Paper-Client": "1"}) as client:
        response = client.post(path, json={}, headers=headers)
        assert response.status_code == 403
        assert "主会话" in response.json()["detail"]
        assert not queued and not store.jobs()


def test_invalid_quote_does_not_persist_or_enqueue(doc):
    with TestClient(app, headers={"X-Paper-Client": "1"}) as client:
        response = client.post("/api/quotes", json={"document_id": doc["id"], "page": 900})
        assert response.status_code == 422
        assert client.get("/api/quotes/nonexistent").status_code == 404
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM quotes").fetchone()[0] == 0
        assert not store.jobs()
