import json
from pathlib import Path

import pytest
from PIL import Image

from paper_video import flow, render, speech


def flow_scene():
    nodes = [
        {"id": "kv", "label": "主分支 KV", "row": 0, "column": 0},
        {"id": "q", "label": "索引器 Q", "row": 0, "column": 1},
        {"id": "scores", "label": "相关性评分", "row": 1, "column": 0},
        {"id": "topk", "label": "Top-K 筛选", "row": 2, "column": 0},
        {"id": "swa", "label": "本层 SWA KV", "row": 2, "column": 1},
        {"id": "attention", "label": "注意力计算", "row": 3, "column": 0},
    ]
    edges = [
        {
            "id": "e1",
            "source": "kv",
            "target": "scores",
            "label": "投影得到 K",
            "kind": "forward",
        },
        {
            "id": "e2",
            "source": "q",
            "target": "scores",
            "label": "查询向量",
            "kind": "forward",
        },
        {
            "id": "e3",
            "source": "scores",
            "target": "topk",
            "label": "相关性得分",
            "kind": "forward",
        },
        {
            "id": "e4",
            "source": "topk",
            "target": "attention",
            "label": "按索引取 KV",
            "kind": "forward",
        },
        {
            "id": "e5",
            "source": "swa",
            "target": "attention",
            "label": "本层 KV",
            "kind": "forward",
        },
    ]

    def step(anchor, action, nodes, edges, input, output):
        return {
            "anchor": anchor,
            "action": action,
            "nodes": nodes,
            "edges": edges,
            "input": input,
            "output": output,
            "condition": "只读取因果可见位置",
            "evidence": "原文明确",
        }

    return {
        "title": "先筛选，再读取",
        "narration": "先看原文。索引器首先评分，利用查询向量和键向量判断相关性。然后执行筛选，保留得分最高的条目。最后共同读取，把选中条目与本层窗口信息交给注意力计算。",
        "bullets": ["先筛选，再读取"],
        "formula": "",
        "diagram_nodes": [],
        "guide_cues": [],
        "flow": {
            "mode": "dynamic",
            "nodes": nodes,
            "edges": edges,
            "steps": [
                step(
                    "索引器首先评分",
                    "计算相关性",
                    ["kv", "q", "scores"],
                    ["e1", "e2"],
                    "索引器 Q 与投影得到的 K",
                    "候选条目的相关性得分",
                ),
                step(
                    "然后执行筛选",
                    "选出 Top-K",
                    ["scores", "topk"],
                    ["e3"],
                    "候选条目的相关性得分",
                    "得分最高的 K 个条目索引",
                ),
                step(
                    "最后共同读取",
                    "联合读取两路 KV",
                    ["topk", "swa", "attention"],
                    ["e4", "e5"],
                    "选中的 KV 与本层 SWA KV",
                    "当前查询的注意力输出",
                ),
            ],
        },
    }


def test_branch_graph_has_aligned_compact_nodes_and_valid_steps():
    scene = flow_scene()
    flow.validate_visuals(scene)
    boxes = flow.layout(scene["flow"])
    assert boxes["kv"][1] == boxes["q"][1]
    assert boxes["kv"][2] < boxes["q"][0]
    assert len(flow.schedule(scene, 24)) == 3
    for edge in scene["flow"]["edges"]:
        points = flow.route(edge, boxes)
        assert points[0] != points[-1]
        for x, y in points:
            assert 805 <= x <= 1242 and 190 <= y <= 480


@pytest.mark.parametrize(
    "change",
    [
        "missing_node",
        "duplicate_position",
        "bad_anchor",
        "missing_step",
        "mixed_legacy",
    ],
)
def test_invalid_mechanisms_fail_before_render(change):
    scene = flow_scene()
    if change == "missing_node":
        scene["flow"]["edges"][0]["source"] = "fictional"
    if change == "duplicate_position":
        scene["flow"]["nodes"][1]["column"] = 0
    if change == "bad_anchor":
        scene["flow"]["steps"][0]["anchor"] = "未说过这句话"
    if change == "missing_step":
        scene["flow"]["steps"].pop()
    if change == "mixed_legacy":
        scene["diagram_nodes"] = ["Q", "K"]
    with pytest.raises(ValueError):
        flow.validate_visuals(scene)


def test_word_boundaries_anchor_mechanism_to_actual_speech():
    text = "先看原文。然后执行筛选，最后读取。"
    boundaries = [
        {"text": "先看原文", "start": 0.1, "duration": 1.2},
        {"text": "然后", "start": 2.1, "duration": 0.4},
        {"text": "执行筛选", "start": 2.5, "duration": 0.6},
        {"text": "最后读取", "start": 4.0, "duration": 1.0},
    ]
    assert flow.anchor_time(text, "然后执行筛选", 20, boundaries) == pytest.approx(2.1)
    assert flow.anchor_time(text, "最后读取", 20, boundaries) == pytest.approx(4.0)


def test_dynamic_graph_changes_state_and_static_graph_still_explains():
    scene = flow_scene()
    times = flow.schedule(scene, 24)
    frames = []
    for at in (times[0] + 1, times[1] + 1, times[2] + 1):
        image = Image.new("RGB", (1280, 720), render.PAPER)
        flow.draw_graph(image, scene, at, times, render.font)
        frames.append(image.tobytes())
    assert len(set(frames)) == 3
    scene["flow"]["mode"] = "static"
    flow.draw_graph(
        Image.new("RGB", (1280, 720), render.PAPER), scene, 0, times, render.font
    )


def test_guiding_cursor_moves_from_evidence_to_current_step():
    scene = flow_scene()
    times = flow.schedule(scene, 24)
    highlights = [(100, 200, 320, 230)]
    first = flow.guide_point(scene, 0, 24, highlights, render.font, times)
    final = flow.guide_point(scene, times[-1] + 1, 24, highlights, render.font, times)
    assert first[0] < 800 and final[0] > 800
    assert final == flow.guide_point(
        scene, times[-1] + 5, 24, highlights, render.font, times
    )


def test_neural_configuration_requires_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "RUNTIME", tmp_path)
    assert speech.profile()["provider"] == "macos"
    (tmp_path / "voice").mkdir()
    (tmp_path / "voice/config.json").write_text(
        json.dumps({"provider": "edge-neural", "voice": "zh-CN-YunxiNeural"})
    )
    settings = speech.profile()
    assert settings["remote_narration"] and settings["speed"] == 1.0
    command = speech.command(settings, Path("input.txt"), Path("out.mp3"))
    assert "--rate=+0%" in command and "zh-CN-YunxiNeural" in command
    assert "[[" not in speech.spoken_text("为什么要筛选？", settings)


def test_neural_failure_does_not_leave_partial_audio_or_change_voice(
    tmp_path, monkeypatch
):
    import asyncio
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "neural_voice", Path(__file__).parents[1] / "scripts/neural_voice.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    called = []

    class BrokenStream:
        def __init__(self, text, voice, **kwargs):
            called.append(voice)

        async def stream(self):
            yield {"type": "audio", "data": b"partial"}
            raise RuntimeError("simulated disconnect")

    async def no_wait(_):
        pass

    monkeypatch.setattr(module.edge_tts, "Communicate", BrokenStream)
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    output = tmp_path / "voice.mp3"
    with pytest.raises(RuntimeError, match="simulated disconnect"):
        asyncio.run(
            module.synthesize(
                "test", "zh-CN-YunxiNeural", "+20%", output, tmp_path / "timing.json"
            )
        )
    assert called == ["zh-CN-YunxiNeural"] * 3
    assert not output.exists() and not list(tmp_path.glob("*.part"))


def test_flow_return_has_a_separate_side_path():
    scene = flow_scene()
    boxes = flow.layout(scene["flow"])
    points = flow.route(
        {"source": "attention", "target": "scores", "kind": "return"}, boxes
    )
    assert len(points) == 6 and (
        points[2][0] < min(b[0] for b in boxes.values())
        or points[2][0] > max(b[2] for b in boxes.values())
    )
    assert points[-1][1] < points[0][1]


def test_new_architecture_cannot_silently_omit_a_diagram():
    scene = {
        "kind": "architecture",
        "narration": "这里介绍流程。",
        "flow": {"mode": "none", "nodes": [], "edges": [], "steps": []},
        "guide_cues": [],
    }
    with pytest.raises(ValueError, match="必须提供真实流程图"):
        flow.validate_visuals(scene)


def test_compact_formula_can_explain_a_flow_without_overflow():
    scene = flow_scene()
    scene["formula"] = "m = 2 → 2m = 4"
    flow.validate_visuals(scene)
    flow.validate_layout(scene, render.font)
    flow.draw_graph(
        Image.new("RGB", (1280, 720), render.PAPER),
        scene, 2, flow.schedule(scene, 24), render.font,
    )
    scene["formula"] = "压缩前后缓存条目数量关系以及条件说明" * 8
    with pytest.raises(ValueError):
        flow.validate_layout(scene, render.font)


def test_new_normal_speed_does_not_override_old_job_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "RUNTIME", tmp_path)
    assert speech.profile()["speed"] == 1.0
    old = {"provider": "edge-neural", "voice": "zh-CN-YunyangNeural", "speed": 1.2, "rate": "+20%"}
    assert "--rate=+20%" in speech.command(old, Path("in.txt"), Path("out.mp3"))
    assert old["speed"] == 1.2
