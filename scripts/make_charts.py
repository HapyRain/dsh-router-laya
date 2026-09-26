# -*- coding: utf-8 -*-
"""Generate the README charts as plain SVG (stdlib only, deterministic, reproducible).

Charts are deliberately AGGREGATE (no per-task detail) per the project's data-sharing stance.
    .venv/Scripts/python.exe scripts/make_charts.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "docs", "charts")

GREEN, AMBER, RED, BLUE, INK, MUTED = "#3fb950", "#d29922", "#f85149", "#58a6ff", "#24292f", "#57606a"


def svg_open(w, h, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" font-family="Segoe UI,Helvetica,Arial,sans-serif">'
            f'<rect x="0" y="0" width="{w}" height="{h}" rx="10" fill="#ffffff" stroke="#d0d7de"/>'
            f'<text x="24" y="38" font-size="17" font-weight="600" fill="{INK}">{title}</text>')


def bar_chart(name, title, series, ymax, footnote, unit="%"):
    w, h = 760, 90 + 40 * len(series) + (34 if footnote else 10)
    s = [svg_open(w, h, title)]
    left, right = 210, w - 90
    scale = (right - left) / ymax
    top = 66
    for i, (label, value, color) in enumerate(series):
        y = top + i * 40
        bw = max(3, value * scale)
        s.append(f'<text x="{left-12}" y="{y+15}" font-size="12.5" fill="{INK}" text-anchor="end">{label}</text>')
        s.append(f'<rect x="{left}" y="{y}" width="{bw:.1f}" height="22" rx="4" fill="{color}"/>')
        vx = min(left + bw + 8, right + 30)
        s.append(f'<text x="{vx:.0f}" y="{y+15}" font-size="12.5" font-weight="600" fill="{color}">{value}{unit}</text>')
    if footnote:
        s.append(f'<text x="24" y="{h-16}" font-size="11.5" fill="{MUTED}">{footnote}</text>')
    s.append("</svg>")
    return "\n".join(s)


charts = {}

# A — why: session failure compounds with the number of must-all-be-correct tasks (live) and the
#    escalation lever cuts it (upper-bound estimate from the live high-arm pair).
charts["chart-why.svg"] = bar_chart(
    "why", "为什么需要自动升档 —— 会话失败率 vs 需全对的任务数",
    [("1 个任务 · low", 4, GREEN),
     ("5 个任务 · low", 16, GREEN),
     ("10 个任务 · low", 30, GREEN),
     ("15 个任务 · low", 42, GREEN),
     ("15 个任务 · 升档 high", 17, AMBER)],
    50,
    "low = 本地实测（k=3）；升档后 = 上界估计（high 臂 2/2 全过，打滑率 ≤1.2%）。数据：hard-pool 试筛 + R1 定点补跑")

# B — judge quality: 7-question fine-tuned classifier vs the 85% ship gate.
charts["chart-accuracy.svg"] = bar_chart(
    "acc", "判定质量 —— 7 题微调分类头 vs 逐字标注金标（383 条）",
    [("Q1 副作用", 98.7, BLUE), ("Q2 跨模块", 99.5, BLUE), ("Q3 步骤依赖", 99.2, BLUE),
     ("Q4 深推理", 99.5, BLUE), ("Q5 代码", 98.4, BLUE), ("Q6 生成", 97.7, BLUE),
     ("Q7 会话复利", 99.0, RED)],
    100,
    "虚线为 85% 上架门槛；全部一次过。金标 = 三模型多数决标注（ds·glm·qwen，双家族一致率 93.7%）")
# 85% gate line
charts["chart-accuracy.svg"] = charts["chart-accuracy.svg"].replace(
    "</svg>",
    f'<line x1="230" y1="66" x2="230" y2="{66+7*40-18}" stroke="{MUTED}" stroke-dasharray="4 3"/>'
    f'<text x="236" y="{66+7*40-24}" font-size="11" fill="{MUTED}">上架门槛 85%</text></svg>')

# C — the live escalation ladder from the real-profile session log (three consecutive turns).
charts["chart-ladder.svg"] = bar_chart(
    "ladder", "实测升级阶梯 —— 真实会话连续三轮（同一任务重试）",
    [("第 1 轮 · 新任务 → low", 1, GREEN),
     ("第 2 轮 · 重试 → high", 2, AMBER),
     ("第 3 轮 · 再重试 → max", 3, RED)],
    3, "live 日志：laya → laya → escalate_regenerate；第 4 轮用户指定 max（intent_force）", unit="")

for name, body in charts.items():
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(body)
    print("wrote", name, len(body), "bytes")
