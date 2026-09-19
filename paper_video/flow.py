"""Compact evidence-backed flow graphs and narration-anchored attention cues."""

import math
import unicodedata
from PIL import ImageDraw

BLUE, PALE, BORDER, DIM = "#3e6d8a", "#e8eff4", "#aebfca", "#a3afb7"


def enabled(scene):
    return scene.get("flow", {}).get("mode", "none") != "none"


def validate_visuals(scene):
    narration = scene["narration"]
    graph = scene.get("flow", {"mode": "none", "nodes": [], "edges": [], "steps": []})
    nodes, edges, steps = graph["nodes"], graph["edges"], graph["steps"]
    if (
        "flow" in scene
        and scene.get("kind") == "architecture"
        and graph["mode"] == "none"
    ):
        raise ValueError("架构/流程场景必须提供真实流程图")
    if graph["mode"] == "none":
        if nodes or edges or steps:
            raise ValueError("无流程图场景不能包含流程节点或步骤")
    else:
        if (
            not 2 <= len(nodes) <= 6
            or not 1 <= len(edges) <= 7
            or not 1 <= len(steps) <= 4
        ):
            raise ValueError("流程图需要 2–6 节点、1–7 条边、1–4 步")
        ids = {n["id"] for n in nodes}
        edge_ids = {e["id"] for e in edges}
        if len(ids) != len(nodes) or len(edge_ids) != len(edges):
            raise ValueError("流程节点或连线 ID 重复")
        positions = set()
        for node in nodes:
            place = (node["row"], node["column"])
            if not 0 <= place[0] <= 3 or place[1] not in (0, 1) or place in positions:
                raise ValueError("流程图节点重叠或超出布局")
            positions.add(place)
            if not node["label"].strip() or len(node["label"]) > 22:
                raise ValueError("流程节点必须使用短名称")
        touched = set()
        for edge in edges:
            if (
                edge["source"] not in ids
                or edge["target"] not in ids
                or edge["source"] == edge["target"]
            ):
                raise ValueError("流程连线端点无效")
            if not edge["label"].strip() or len(edge["label"]) > 16:
                raise ValueError("流程连线缺少简短数据/动作标签")
            touched.update((edge["source"], edge["target"]))
        if touched != ids:
            raise ValueError("流程图存在无连接的节点")
        covered_nodes, covered_edges = set(), set()
        previous = -1
        for step in steps:
            at = unique_anchor(narration, step["anchor"])
            if at <= previous:
                raise ValueError("流程步骤必须按旁白顺序排列")
            previous = at
            if (
                not step["nodes"]
                or not set(step["nodes"]) <= ids
                or not set(step["edges"]) <= edge_ids
            ):
                raise ValueError("流程步骤引用了无效节点或连线")
            covered_nodes.update(step["nodes"])
            covered_edges.update(step["edges"])
            for key, limit in [
                ("action", 30),
                ("input", 38),
                ("output", 38),
                ("condition", 38),
            ]:
                if not step[key].strip() or len(step[key]) > limit:
                    raise ValueError("流程步骤说明缺失或过长：" + key)
        if covered_nodes != ids or covered_edges != edge_ids:
            raise ValueError("流程步骤未覆盖所有节点与连线")
        if scene.get("diagram_nodes"):
            raise ValueError("流程图不能与旧节点图混用")
    previous = -1
    cues = scene.get("guide_cues", [])
    if "guide_cues" in scene and graph["mode"] == "none" and not cues:
        raise ValueError("非流程场景也需要明确的鼠标引导锚点")
    if len(cues) > 4:
        raise ValueError("鼠标引导每场最多 4 个重点")
    for cue in cues:
        at = unique_anchor(narration, cue["anchor"])
        if at <= previous:
            raise ValueError("鼠标引导必须按旁白顺序排列")
        previous = at
        if cue["target"] == "bullet" and not 0 <= cue["index"] < len(scene["bullets"]):
            raise ValueError("鼠标引导要点不存在")
        if cue["target"] == "formula" and not scene.get("formula"):
            raise ValueError("鼠标指向了不存在的公式")
        if cue["target"] != "bullet" and cue["index"] != 0:
            raise ValueError("原文和公式引导索引必须为 0")


def unique_anchor(text, anchor):
    if not anchor.strip() or text.count(anchor) != 1:
        raise ValueError("动画旁白锚点必须在配音中准确且唯一出现：" + anchor)
    return text.index(anchor)


def canonical(text):
    return "".join(
        c for c in unicodedata.normalize("NFKC", text).lower() if c.isalnum()
    )


def anchor_time(narration, anchor, duration, boundaries=()):
    index = unique_anchor(narration, anchor)
    target = len(canonical(narration[:index]))
    text = canonical(narration)
    cursor = 0
    for boundary in boundaries:
        word = canonical(boundary["text"])
        if not word:
            continue
        start = text.find(word, cursor)
        if start < 0:
            continue
        end = start + len(word)
        cursor = end
        if target < end:
            fraction = max(0, (target - start) / len(word))
            return min(duration, boundary["start"] + fraction * boundary["duration"])
    return duration * index / max(1, len(narration))


def schedule(scene, duration, boundaries=()):
    steps = scene.get("flow", {}).get("steps", [])
    return [
        min(
            max(
                1.7, anchor_time(scene["narration"], s["anchor"], duration, boundaries)
            ),
            max(0, duration - 0.2),
        )
        for s in steps
    ]


def layout(graph):
    result = {}
    for node in graph["nodes"]:
        same_row = [n for n in graph["nodes"] if n["row"] == node["row"]]
        x = 940 if len(same_row) == 1 else 823 + node["column"] * 220
        y = 207 + node["row"] * 72
        result[node["id"]] = (x, y, x + 174, y + 36)
    return result


def route(edge, boxes, index=0):
    a, b = boxes[edge["source"]], boxes[edge["target"]]
    ax, ay, bx, by = (a[0] + a[2]) / 2, a[3], (b[0] + b[2]) / 2, b[1]
    if abs(a[1] - b[1]) < 1:
        if a[0] < b[0]:
            return [(a[2], a[1] + 18), (b[0], b[1] + 18)]
        return [(a[0], a[1] + 18), (b[2], b[1] + 18)]
    if edge["kind"] == "return" or by < ay or by - ay > 65:
        lane = 815 if bx < 1027 or (bx == 1027 and index % 2 == 0) else 1230
        # Exit into row gaps before moving sideways, never through a sibling node
        return [
            (ax, ay),
            (ax, ay + 10),
            (lane, ay + 10),
            (lane, by - 10),
            (bx, by - 10),
            (bx, by),
        ]
    mid = (ay + by) / 2
    return [(ax, ay), (ax, mid), (bx, mid), (bx, by)]


def label_positions(graph, boxes, font):
    """Place labels beside paths, avoiding every node, line and previous label."""
    paths = [route(edge, boxes, i) for i, edge in enumerate(graph["edges"])]
    occupied = []
    for box in boxes.values():
        occupied.append((box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1))
    for points in paths:
        for a, b in zip(points, points[1:]):
            occupied.append(
                (
                    min(a[0], b[0]) - 1,
                    min(a[1], b[1]) - 1,
                    max(a[0], b[0]) + 1,
                    max(a[1], b[1]) + 1,
                )
            )
    labels = []

    def overlaps(a, b):
        return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

    for edge, points in zip(graph["edges"], paths):
        width = font(13).getlength(edge["label"])
        candidates = []
        segments = sorted(
            zip(points, points[1:]), key=lambda pair: math.dist(*pair), reverse=True
        )
        for a, b in segments:
            for fraction in (0.5, 0.25, 0.75):
                x, y = point_on_path([a, b], fraction)
                if a[0] == b[0]:
                    candidates.extend([(x + 7, y - 7), (x - width - 7, y - 7)])
                else:
                    candidates.extend([(x - width / 2, y - 17), (x - width / 2, y + 3)])
        choice = None
        for x, y in candidates:
            box = (x, y, x + width, y + 15)
            if x < 818 or box[2] > 1242 or y < 184 or box[3] > 475:
                continue
            if not any(overlaps(box, other) for other in occupied):
                choice = (x, y)
                occupied.append((x - 2, y - 2, x + width + 2, y + 17))
                break
        if choice is None:
            raise ValueError("流程连线标签拥挤，请拆分场景或缩短标签：" + edge["label"])
        labels.append(choice)
    return paths, labels


def point_on_path(points, fraction):
    distances = [math.dist(a, b) for a, b in zip(points, points[1:])]
    remaining = max(0, min(1, fraction)) * sum(distances)
    for a, b, distance in zip(points, points[1:], distances):
        if distance and remaining <= distance:
            f = remaining / distance
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        remaining -= distance
    return points[-1]


def arrow(draw, points, color, dashed=False):
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        if dashed and length:
            for start in range(0, int(length), 10):
                p = point_on_path([a, b], start / length)
                q = point_on_path([a, b], min(1, (start + 5) / length))
                draw.line((*p, *q), fill=color, width=2)
        else:
            draw.line((*a, *b), fill=color, width=2)
    b = points[-1]
    a = next((p for p in reversed(points[:-1]) if p != b), points[0])
    angle = math.atan2(b[1] - a[1], b[0] - a[0])
    draw.polygon(
        [
            b,
            (b[0] - 7 * math.cos(angle - 0.5), b[1] - 7 * math.sin(angle - 0.5)),
            (b[0] - 7 * math.cos(angle + 0.5), b[1] - 7 * math.sin(angle + 0.5)),
        ],
        fill=color,
    )


def short_lines(text, font, width):
    lines = [""]
    for char in text:
        if font.getlength(lines[-1] + char) > width:
            lines.append(char)
        else:
            lines[-1] += char
    return lines


def validate_layout(scene, font):
    if not enabled(scene):
        return
    graph = scene["flow"]
    if scene.get("formula") and len(short_lines(scene["formula"], font(16), 420)) > 1:
        raise ValueError("长公式应与流程图分场景展示")
    label_positions(graph, layout(graph), font)
    for node in graph["nodes"]:
        if len(short_lines(node["label"], font(16), 158)) > 2:
            raise ValueError("流程节点名称过长，请使用已定义的标准缩写")
    for step in graph["steps"]:
        if font(17).getlength("4/4  " + step["action"]) > 420:
            raise ValueError("流程动作说明超出画面，请精简")
        for key in ("input", "output", "condition"):
            if font(14).getlength("条件  " + step[key]) > 420:
                raise ValueError("流程图外说明超出画面，请精简：" + key)


def draw_graph(frame, scene, seconds, times, font):
    graph = scene["flow"]
    boxes = layout(graph)
    draw = ImageDraw.Draw(frame)
    if scene.get("formula"):
        draw.text((818, 181), scene["formula"], font=font(16), fill=BLUE)
    active = max((i for i, t in enumerate(times) if seconds >= t), default=-1)
    static = graph["mode"] == "static"
    completed = set(
        e for step in graph["steps"][: max(0, active)] for e in step["edges"]
    )
    current = graph["steps"][max(0, active)]
    moving_edges = set(current["edges"]) if active >= 0 else set()
    paths, labels = label_positions(graph, boxes, font)
    for index, edge in enumerate(graph["edges"]):
        points = paths[index]
        color = (
            BLUE
            if static or edge["id"] in completed or edge["id"] in moving_edges
            else BORDER
        )
        arrow(draw, points, color, edge["kind"] == "optional")
        draw.text(labels[index], edge["label"], font=font(13), fill=BLUE)
        if not static and edge["id"] in moving_edges:
            fprogress = min(1, max(0, (seconds - times[active]) / 0.85))
            if fprogress < 1:
                x, y = point_on_path(points, fprogress)
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=BLUE)
    for node in graph["nodes"]:
        box = boxes[node["id"]]
        on = active >= 0 and node["id"] in current["nodes"]
        draw.rounded_rectangle(
            box,
            radius=4,
            fill="#dceaf3" if on else PALE,
            outline=BLUE if on else BORDER,
            width=2 if on else 1,
        )
        rows = short_lines(node["label"], font(16), 158)
        if len(rows) > 2:
            raise ValueError("流程节点文字超出两行：" + node["label"])
        y = box[1] + (36 - len(rows) * 17) / 2
        for row in rows:
            draw.text(
                ((box[0] + box[2] - font(16).getlength(row)) / 2, y),
                row,
                font=font(16),
                fill="#294453",
            )
            y += 17
    title = f"{max(0, active) + 1}/{len(times)}  {current['action']}"
    draw.text((818, 479), title, font=font(17), fill=BLUE)
    details = [
        ("输入", current["input"]),
        ("输出", current["output"]),
        ("条件", current["condition"]),
    ]
    y = 508
    for label, value in details:
        rows = short_lines(label + "  " + value, font(14), 420)
        if len(rows) > 1:
            raise ValueError("流程图外说明过长：" + value)
        draw.text((818, y), rows[0], font=font(14), fill="#465963")
        y += 21
    draw.text((818, 576), "证据  " + current["evidence"], font=font(13), fill="#68746c")
    # Overall step timeline persists while the current state is emphasized
    for i in range(len(times)):
        x = 821 + i * 27
        draw.ellipse((x, 599, x + 7, 606), fill=BLUE if i <= active else BORDER)
    if active < 0:
        return None
    box = boxes[current["nodes"][-1]]
    return (box[2] - 7, box[1] + 7)


def cursor(draw, point, seconds):
    x, y = point
    x = max(8, min(1251, x))
    y = max(12, min(588, y))
    r = 10 + 1.5 * math.sin(seconds * 3)
    draw.ellipse((x - r, y - r, x + r, y + r), outline="#6b94b0", width=2)
    shape = [
        (x, y),
        (x + 2, y + 22),
        (x + 8, y + 16),
        (x + 13, y + 25),
        (x + 17, y + 23),
        (x + 12, y + 14),
        (x + 21, y + 13),
    ]
    draw.polygon(shape, fill="#ffffff", outline="#243d4c", width=2)


def guide_point(scene, seconds, duration, highlights, font, times, boundaries=()):
    source = (highlights[-1][2] + 5, highlights[-1][3] + 3) if highlights else (70, 170)
    cues = [(0.0, source)]
    if enabled(scene):
        boxes = layout(scene["flow"])
        for at, step in zip(times, scene["flow"]["steps"]):
            box = boxes[step["nodes"][-1]]
            cues.append((at, (box[2] - 7, box[1] + 7)))
    else:
        bullet_points = []
        y = 195
        for bullet in scene["bullets"]:
            bullet_points.append((819, y + 7))
            y += len(short_lines(bullet, font(19), 386)) * int(19 * 1.45) + 12
        for cue in scene.get("guide_cues", []):
            point = (
                source
                if cue["target"] == "source"
                else (820, y + 20)
                if cue["target"] == "formula"
                else bullet_points[cue["index"]]
            )
            cues.append(
                (
                    anchor_time(
                        scene["narration"], cue["anchor"], duration, boundaries
                    ),
                    point,
                )
            )
    cues.sort(key=lambda c: c[0])
    active = max(i for i, c in enumerate(cues) if c[0] <= seconds)
    at, target = cues[active]
    previous = cues[max(0, active - 1)][1]
    progress = max(0, min(1, (seconds - at) / 0.35))
    progress = progress * progress * (3 - 2 * progress)
    return (
        previous[0] + (target[0] - previous[0]) * progress,
        previous[1] + (target[1] - previous[1]) * progress,
    )
