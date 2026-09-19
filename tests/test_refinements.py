import re

import pymupdf as fitz
import pytest

from paper_video import papers, render, speech, store


@pytest.fixture
def marked_doc(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    store.init()
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 100), "The main KV entries are selected.")
        page.insert_text((50, 220), "The main KV entries are selected.")
        page.insert_text((50, 300), "Compression uses over-\nlapping source entries.")
        return papers.ingest(pdf.tobytes(), "marks.pdf")


def test_repeated_quote_uses_selected_occurrence(marked_doc):
    marks = papers.quote_marks(
        marked_doc["id"], 1, "main KV entries", [40, 200, 400, 235]
    )
    assert len(marks) == 1
    assert 200 < marks[0]["rect"][1] < 220
    assert marks[0]["underline_y"] == pytest.approx(221.32, abs=0.01)
    assert marks[0]["rect"][2] - marks[0]["rect"][0] < 90


def test_hyphenation_keeps_two_precise_lines(marked_doc):
    marks = papers.quote_marks(marked_doc["id"], 1, "overlapping source entries")
    assert len(marks) == 2
    assert marks[0]["rect"][0] > marks[1]["rect"][0]
    assert marks[0]["underline_y"] < marks[1]["underline_y"]


def test_missing_quote_never_underlines_entire_paragraph(marked_doc):
    scene = {"source_page": 1, "highlight_quote": "not in this paper", "title": "Test",
             "kind": "concept", "bullets": [], "formula": "", "diagram_nodes": []}
    _, marks = render.background(
        marked_doc["id"], {"page": 1, "bbox": [50, 200, 400, 235]}, scene, 0, 1, ""
    )
    assert marks == []
    assert not papers.quote_marks(marked_doc["id"], 1, "")


def test_quote_outside_selection_or_ambiguous_is_not_marked(marked_doc):
    assert not papers.quote_marks(marked_doc["id"], 1, "main KV entries", [40, 280, 400, 335])
    assert not papers.quote_marks(marked_doc["id"], 1, "main KV entries")
    # Every line must belong to the selected range, not merely overlap its bounds
    assert not papers.quote_marks(marked_doc["id"], 1, "overlapping source entries", [40, 285, 400, 305])


def test_underline_finishes_quickly_and_has_constant_line_speed():
    lines = [(10, 0, 310, 30), (10, 40, 110, 70)]
    assert not render.highlight_segments(lines, 0.1)
    assert len(render.highlight_segments(lines, 0.5)) == 1
    finished = render.highlight_segments(lines, 1.7)
    assert finished == [(10, 30, 310, False), (10, 70, 110, False)]
    assert render.highlight_segments(lines, 100) == finished
    assert not render.highlight_segments([], 100)


def test_prosody_retains_words_and_balances_pitch():
    text = "为什么要用 KV 缓存？但压缩不等于无损。这里保留最近的信息。"
    result = speech.spoken_text(text, {"provider": "macos", "prosody": "classroom-v1"})
    assert re.sub(r"\[\[.*?\]\]", "", result) == text
    assert "pbas +1.2" in result and "pbas -1.2" in result
    assert "rate 176" in result and "slnc 140" in result
    assert result.endswith("[[rate 185]]")


def test_untrusted_text_cannot_inject_speech_controls():
    text = "请读 [[rate 0]] Q 和 K"
    for settings in [
        {"provider": "macos", "prosody": "classroom-v1"},
        {"provider": "qwen-reference"},
    ]:
        assert "[[rate 0]]" not in speech.spoken_text(text, settings)
    assert speech.spoken_text("原文", {"provider": "qwen-reference"}) == "原文"


def test_local_voice_controls_do_not_get_spoken(tmp_path):
    import shutil

    if not shutil.which("say"):
        pytest.skip("macOS speech engine required")
    text = "为什么要保留滑动窗口？但压缩不等于无损。这里仍然保留最近的信息。"
    settings = {"provider": "macos", "voice": "Tingting", "speed": 1.2}
    _, plain = render.synthesize(text, tmp_path, 0, lambda: False, settings)
    _, expressive = render.synthesize(
        text, tmp_path, 1, lambda: False, {**settings, "prosody": "classroom-v1"}
    )
    # Unsupported combined/format directives were read aloud and almost doubled duration
    assert 0.85 < expressive / plain < 1.25
    assert "[[pbas +1.2]]" in (tmp_path / "scene-01.speech.txt").read_text()
    assert (tmp_path / "scene-00.txt").read_text() == (
        tmp_path / "scene-01.txt"
    ).read_text()
