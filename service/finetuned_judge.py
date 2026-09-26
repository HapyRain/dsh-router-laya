"""微调路由模型的 laya判定层：6 个 noul -> 规则引擎 -> `low|high|max`。

**两套协议，不能混用。**

| | 基座 `convaiinnovations/laya` | 微调 `training/laya_router_finetuned` |
|---|---|---|
| 加载 | `laya.load(repo)` | `laya.Agent(local_dir)` |
| 问题 | `laya.router_questions()`：4 类 `score` + `choice` + 2 `noul` | 下面这 **6 个 `noul`** |
| 输出 | `difficulty` 0–3 的**期望值** | 6 个布尔 + 规则引擎 |
| 档位 | `trivial/easy/moderate/hard`（4 档，且顶档够不到） | **直接 `low/high/max`**（3 档） |

拿基座的问题去问微调模型（或反过来）不会报错，只会得到**看着像模像样的垃圾**——所以走哪条路必须
由代码决定，不能靠环境变量"碰运气"。

微调路径**不需要归一化**：它直接吐 `low|high|max`，落档就是一张 1:1 对照表。之前为基座 4 分类头
搭的那套分位归一化（`auto_mode_sim.resolve_norm`）在这条路上没有意义。

    $env:LAYA_MODEL = 'D:\\tmp\\st\\laya\\training\\laya_router_finetuned'
    .venv/Scripts/python.exe routing/demo_auto_mode.py

`compute_tier()` 的规则与权重是**策略**，只有这一个定义。原先 `training/` 里散着三份拷贝，规则互不相同
（`test_finetuned.py` / `generate_dataset.py` / `merge_final.py`），现在都改成从这里导入——那三份的权重
与阈值跟运行时并不一致，等于用一套规则生成训练数据、再用另一套规则在运行时落档。

Q7 extension (2026-09, plan `docs/knob-cross-validation-product-plan.md` §3.2/§3.3): QUESTIONS gains
a seventh noul for the session-compounding axis — success requiring multiple *independent*
sub-results to all be correct (session failure ~ 1-(1-p)^N). Q1-Q6 (single-task structural
complexity) stay byte-identical; ids are a stable contract, so Q7 is appended, never inserted. The
deployed checkpoint has NOT been trained on Q7, so its Q7 answer is gated off in judge()
(`Q7_MODEL_SOURCE_READY`) until a Q7-fine-tuned checkpoint is deployed.
"""
import time

# 6 个路由问题。ids 是稳定契约：规则引擎和权重表都按它们取值，改名等于改协议。
QUESTIONS = {
    "Q1": {"type": "noul", "instructions": "To complete this request, would the assistant need to "
                                           "change anything outside the conversation (modify files, "
                                           "run code, call services)?"},
    "Q2": {"type": "noul", "instructions": "Does this involve integration or architectural changes "
                                           "spanning multiple modules, services, or phases?"},
    "Q3": {"type": "noul", "instructions": "If an early step turns out wrong, would later steps be "
                                           "affected (some steps must finish and be verified before "
                                           "others start)?"},
    "Q4": {"type": "noul", "instructions": "Does this require multi-step reasoning, solving "
                                           "non-trivial constraints, or open-ended "
                                           "diagnosis/optimization without clear acceptance criteria "
                                           "(mathematics, logic, planning, algorithms, performance "
                                           "debugging)?"},
    "Q5": {"type": "noul", "instructions": "Does this involve reading, writing, or modifying source "
                                           "code files?"},
    "Q6": {"type": "noul", "instructions": "Does this require generating new content (code, analysis, "
                                           "report, design) rather than retrieving or summarizing "
                                           "existing information?"},
    # Q7 (session-compounding axis, plan §3.2): about N *independent* sub-results that must ALL be
    # correct — deliberately not "how complex is one task" (that is Q2/Q3/Q4). Appended last;
    # Q1-Q6 text and order are a frozen contract (rule engine, weight table and fine-tune training
    # all key off these ids).
    "Q7": {"type": "noul", "instructions": "Does success require multiple independent sub-results "
                                           "to all be correct (a batch of changes, edits across "
                                           "several files, or a long sequence of items each "
                                           "checked), so that any single wrong sub-result makes "
                                           "the whole request fail?"},
}

# 只在规则 5/6 用得上：规则 1–4 只看布尔组合。所以调准确率的杠杆在规则顺序和 Q3 的阈值上，
# 不在权重表上。
# Q7 is deliberately absent: rule 0 is a hard pre-override (Q7 -> high, never max), not a weighted
# vote — its all-or-nothing semantics carry no magnitude here, the threshold lives upstream (N x p).
WEIGHTS = {"Q1": 0.18, "Q2": 0.18, "Q3": 0.12, "Q4": 0.29, "Q5": 0.09, "Q6": 0.14}

TIERS = ["low", "high", "max"]

# Q7 model-source gate: the deployed fine-tuned checkpoint was trained on Q1-Q6 only, so its Q7
# answer is exactly the "plausible garbage" the module docstring warns about. Keep False until a
# checkpoint fine-tuned with Q7 is deployed; flip to True to let the model's own noul answer feed
# labels. While False, judge() forces labels["Q7"] = False (key kept: the seven-key shape stays
# stable for the rule engine and its tests) and tiering is identical to the pre-Q7 six-rule
# behavior.
#
# ENABLED 2026-09-25: the laya-router-7q checkpoint (383-text corpus, majority-vote Q7 labels
# under ruling B, trained locally on the RTX 4070 SUPER) answers Q7 at 99.0% accuracy /
# kappa 0.956 vs backfill gold with no Q1-Q6 regression (training/q7_validation_report.json).
Q7_MODEL_SOURCE_READY = True

# 意图解析已移到 intent_parser.py（Phase 0: 词典锚点 + 否定作用域 + 约束代数）。
# laya判定只调 parse_intent / apply_constraint，不自己做正则匹配。


def compute_tier(labels):
    """六条有序规则：布尔组合 -> 档位。顺序即优先级，先命中先返回。

    实测 11 条里错的两条都由 Q3（步骤依赖）翻掉决定：`重构 microservices` 因 Q3=NO 落到规则 4、
    `部署 EC2` 因 Q3=YES 命中规则 2。想提准确率先动这两条规则的边界或 Q3 的阈值。

    Rule 0 (Q7, prepended 2026-09 before the six legacy rules): a YES on Q7 — success requires
    multiple *independent* sub-results to all be correct (batch changes, multi-file edits, a long
    checked sequence) — returns "high" immediately. Rationale (plan
    `docs/knob-cross-validation-product-plan.md` §3.2): this is the session-compounding axis,
    session failure ~ 1-(1-p)^N with p the single-task slip rate; whether that crosses the
    escalation threshold is decided upstream from N x p, so this layer only consumes the boolean
    and floors the tier at high. Q7 never yields max (§3.3: max stays the escalation ladder's
    second rung; the feed-forward never predicts max). `labels.get("Q7")` keeps six-key dicts from
    pre-Q7 callers behaving exactly as before.
    """
    q1, q2, q3, q4, q5, q6 = (labels[q] for q in ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"])

    # Rule 0: Q7 compounding trigger -> high, never max. Rationale in the docstring above.
    if labels.get("Q7"):
        return "high"

    # 规则1: 纯推理（Q4 无代码/副作用）→ max
    if q4 and not q1:
        return "max"

    # 规则2: 多模块依赖链（Q2+Q3）→ max
    if q2 and q3:
        return "max"

    # 规则3: 有代码+推理但无跨模块（Q1+Q4+Q5，单文件调试/算法）→ high
    if q1 and q4 and q5 and not q2:
        return "high"

    # 规则4: Q2 跨模块但无强依赖（部署/配置类）→ high
    if q2 and not q3:
        return "high"

    # 规则5: 有副作用（Q1）→ 至少 high，加权够高则 max
    if q1:
        score = sum(WEIGHTS[q] for q in WEIGHTS if labels[q])
        if score >= 0.60:
            return "max"
        return "high"

    # 规则6: 基础加权
    score = sum(WEIGHTS[q] for q in WEIGHTS if labels[q])
    if score >= 0.40:
        return "high"
    return "low"


def load(path, device=None):
    """The fine-tune is a local directory, so it loads through `Agent` rather than `laya.load`."""
    import laya

    return laya.Agent(path, device=device) if device else laya.Agent(path)


# ── C3 escalation (plugin v2, docs/plugin-v2-plan.md §3.1) ──────────────────────────────
# Strategy-layer concern, deliberately NOT in intent_parser: the frozen dictionary answers
# "what tier does the user want"; regenerate answers "did the previous turn fail". Two
# different constructs (Q-decisions freeze covers the dictionary only).

def escalate(tier):
    """One rung up the C3 ladder, capped at max (the feed-forward never predicts max; max is
    reachable only through escalation).

    Non-TIERS rungs happen: prev_tier can be a qwen `medium` (see the M1 note in judge()).
    Map the known one to its nearest TIERS rung; anything unknown returns UNCHANGED so the
    caller's off-ladder floor check simply ignores it -- never crash, never invent a rung
    (deepseek cross-review F2).
    """
    return {"low": "high", "high": "max", "max": "max", "medium": "high"}.get(tier, tier)


def _floor_tier(a, b):
    """The higher of two TIERS rungs; an off-ladder `b` is ignored (returns `a`)."""
    if b in TIERS and TIERS.index(b) > TIERS.index(a):
        return b
    return a


_REGENERATE_WORDS = ("重新", "再来", "重试", "again", "regenerate", "redo")
# Correction words are NOT retries on their own ("不对，我是说 X" is a clarification) -- they
# count only when followed by an explicit retry verb (deepseek cross-review F8: a mis-fired
# escalation is permanent on a monotonic ladder, so the trigger must be conservative).
_REGENERATE_CORRECTIONS = ("不对", "还是错")


def is_regenerate(text, prev_task):
    """Whether THIS message retries the PREVIOUS task (C3 escalation signal).

    Two criteria, either fires:
      1. similarity: normalized prev_task equals or prefixes normalized text (DSH resend and
         the refine-after-dissatisfaction shape; the known bias -- an extended new task
         escalates one rung -- is accepted product semantics, plan §6);
      2. retry phrasing at the head of the message, with corrections requiring an explicit
         retry verb.
    None/empty prev_task is always False: turn 1 must never crash into the silent fail-safe
    (deepseek cross-review F3).
    """
    if not text or not prev_task:
        return False

    def norm(s):
        return "".join(s[:2000].lower().split())

    t, p = norm(text), norm(prev_task)
    if t == p or t.startswith(p):
        return True
    head = t[:12]
    if any(head.startswith(w) for w in _REGENERATE_WORDS):
        return True
    if any(head.startswith(c) for c in _REGENERATE_CORRECTIONS) and \
            any(w in t for w in _REGENERATE_WORDS):
        return True
    return False


def judge(agent, text, prev_tier=None, prev_task=None):
    """One task -> one tier plus the label booleans that produced it (Q1-Q7; Q7 model-gated by
    `Q7_MODEL_SOURCE_READY`).

    三层决策：
      Phase 0: 意图解析（词典锚点 + 否定作用域 + 继承检测，零延迟）
        → force → 直接定档（一票否决）
        → inherit → 保持 prev_tier（"继续"等短指令不降档）
        → exclude → 进入 Laya 判断，用约束过滤 base_tier
      Phase 1: Laya 7 题判断 → 规则引擎（含规则 0）→ base_tier
      C3: regenerate 检测 → base_tier 抬底到 escalate(prev_tier)（升级先行）
      融合: apply_constraint(base_tier, intent, prev_tier) —— 约束代数是最后一道闸，
            升级结果同样被 exclude 过滤（"别用 max" 压得住升级；交叉评审 F1/F5）

    prev_tier: 上一轮的档位，inherit 意图时使用。None = 首轮。
    prev_task: 上一轮的任务文本（plugin v2 会话状态），regenerate 检测用。None = 无历史。
    """
    # ── Phase 0: 意图解析（零延迟，词典+否定+继承）──
    from intent_parser import parse_intent, apply_constraint
    intent = parse_intent(text)

    # force 意图 = 一票否决，不走 Laya
    if intent is not None and intent["op"] == "force":
        return {
            "tier": intent["tier"],
            "labels": {},
            "probs": {},
            "yes": [],
            "ms": 0,
            "triggered_by": "intent_force",
            "regenerate": False,
            "intent": intent,
        }

    # inherit 意图 = 保持上一轮档位，不走 Laya。
    # Echo prev_tier VERBATIM (M1): the host stores the rung's effort as prev_tier, which on a
    # non-TIERS ladder (qwen `medium`, …) is outside low|high|max. Host validates against
    # TIERS ∪ current-ladder efforts (tierIsLegal), not TIERS alone — do not clamp here.
    if intent is not None and intent["op"] == "inherit":
        return {
            "tier": prev_tier or "low",   # 没有上一轮 → 回退 low
            "labels": {},
            "probs": {},
            "yes": [],
            "ms": 0,
            "triggered_by": "intent_inherit",
            "regenerate": False,
            "intent": intent,
            "prev_tier": prev_tier,
        }

    # ── Phase 1: Laya 6 题判断 ──
    t0 = time.time()
    result = agent.predict(text, QUESTIONS)
    labels = {}
    probs = {}
    for qid in QUESTIONS:
        ans = result["answers"][qid]
        probs[qid] = ans["noul"]
        labels[qid] = ans["noul"] >= 0.5
    if not Q7_MODEL_SOURCE_READY:
        # Untrained Q7 answer suppressed; probs keep the raw value for diagnostics only.
        labels["Q7"] = False
    base_tier = compute_tier(labels)

    # ── C3 escalate: a detected retry floors base_tier at escalate(prev_tier) (plan §3.1).
    # Escalation runs BEFORE apply_constraint so the constraint algebra stays the LAST gate --
    # an exclusion ("别用 max") must bind the escalated tier too (deepseek review F1/F5).
    # Off-ladder escalate results (unknown prev_tier) are ignored by _floor_tier.
    # `regenerate` is reported even when the floor does not move the tier (diagnostic: the
    # plugin log should show that a retry was seen), while triggered_by names what actually
    # determined the tier.
    regen_detected = prev_tier is not None and is_regenerate(text, prev_task)
    escalated = False
    if regen_detected:
        floored = _floor_tier(base_tier, escalate(prev_tier))
        escalated = floored != base_tier
        base_tier = floored

    # ── 融合: 约束过滤（exclude 等）──
    final_tier = apply_constraint(base_tier, intent, prev_tier)

    # Phase 1 always ran here. Only a real `exclude` constraint filtered base_tier;
    # `op=none` (and any future fall-through) is the content judgment speaking for
    # itself. P0 made parse_intent always return a dict, so the old
    # `"intent_exclude" if intent else "laya"` was truthy for none and mislabelled it.
    op = intent.get("op") if isinstance(intent, dict) else None
    if escalated:
        triggered_by = "escalate_regenerate"
    elif op == "exclude":
        triggered_by = "intent_exclude"
    else:
        triggered_by = "laya"
    return {
        "tier": final_tier,
        "base_tier": base_tier,
        "labels": labels,
        "probs": probs,
        "yes": [q for q in QUESTIONS if labels[q]],
        "ms": round((time.time() - t0) * 1000),
        "triggered_by": triggered_by,
        "regenerate": regen_detected,
        "intent": intent,
    }


def rung_index(tier, ladder):
    """`low|high|max` -> rung index on the single ladder.

    单线三档（已砍掉经济/质量两线和 off 档）：
    low=#0, high=#1, max=#2。直接位置映射，不需要归一化。
    Clamped，因为短阶梯不能超过自身长度。
    """
    if not ladder:
        return None
    return min(len(ladder) - 1, max(0, TIERS.index(tier)))
