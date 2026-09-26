"""Phase 0 意图解析器：词典锚点 + 否定作用域 + 约束代数（零训练，零延迟）。

设计来源：docs/intent-detection-review.md 三模型综合定稿。

三层结构：
  1. 锚点匹配：在文本中找档位词（语义族，不是穷举）
  2. 否定作用域：检查锚点左侧窗口有无否定词
  3. 约束代数：输出 {op, tier, span} 结构化约束

输出是**证据**，不是决策。最终档位由 finetuned_judge.compute_tier +
本模块的约束过滤共同决定。

    from intent_parser import parse_intent, apply_constraint

    intent = parse_intent("不要用最高思考，简单做")
    # → {'op': 'exclude', 'tier': 'max', 'span': '不要用最高', 'polarity': 'negate'}

    final = apply_constraint('high', intent)
    # → 'high'（exclude(max) 不影响 high）
"""

import re
from typing import Dict, List, Optional


# ── 锚点词表 ──────────────────────────────────────────────────────────────────────────────────────
# 每档一个语义族，覆盖常见表达。匹配靠这些词触发，不靠穷举所有说法。
# 新表达如果 embedding 能落到这些锚点附近，Phase 1 的模型就能泛化。

ANCHORS = {
    "max": [
        # ── 原 max 词（v1 保留）──
        "最高思考", "最高强度", "最大思考", "最强思考", "最高effort", "最大effort",
        "拉满", "火力全开", "开到最大", "尽最大努力", "最深度", "最强模型",
        "最拉的", "最顶级", "顶配", "满血",
        "max effort", "max thinking", "think hard", "think deep", "think max",
        "maximum effort", "maximum thinking", "go all out", "full power",
        "深度思考到底", "想透彻", "想到底",
        # ── M2 质量强化族（裁决1：从 high 迁入 max）──
        "认真分析", "认真推敲", "认真推演", "认真想", "认真做", "认真点",
        "认真思考", "认真推理", "认真核对", "认真检查", "认真评估",
        "仔细分析", "仔细推演", "仔细推敲", "仔细想", "仔细检查", "仔细核对",
        "深度思考", "深度分析", "深度推理", "深度模式", "深度调研", "深度剖析",
        "深入思考", "深入分析", "深入调研", "深入剖析", "深入",
        "彻底", "透彻", "严谨", "缜密", "慎重", "深思熟虑", "周密",
        # 想清楚/想明白/推敲：GRAY→按金标 majority 收在 high（过程增量），勿放 max
        # 推演仍归 max（金标「推演一遍」等为 force_max）
        "想透", "推演",
        # think harder → force_high（裁决 Q1 增量词，勿放 max）
        "think deeply", "think carefully", "think hard",
        "deep dive", "go deep", "thoroughly", "thorough",
        # ── 质量/穷尽补漏（§7 金标漏检）──
        # 裸词 careful 会 FP（"careful about the details"）；只留完整形态。
        # 裸「每个假设/把每个假设」会 FP（列出来给客户看）；用验证类形态锚定。
        "careful analysis", "a careful analysis",
        "验证一遍", "把每个假设都验证", "都验证一遍",
        "check each assumption", "verify every", "verify each",
        # 泛化：核一遍/每条推导（probe 改写，非金标原句）
        "核一遍", "每条推导", "都核一遍",
        # exclude 目标档位名（否定后 → exclude，非 none）
        "top 档", "top档", "top 档思考",
        "很重的思考", "重的思考模式", "heavy thinking", "heavy thinking mode",
        "deliberate", "be deliberate", "rigorous",
        "deep thinking", "deep reasoning", "deepest thinking",
        "think it through", "think it over", "reason carefully",
        "think this through properly", "think long and hard",
        "airtight", "deep-dive thinking", "deep-dive",
        "deeply", "in depth", "seriously",
        # ── 补充缺口词（三方诊断 P1）──
        "最深思", "最深思考", "深思考", "深想", "极限思考", "全功率", "全速",
        "最猛", "顶格", "顶满", "满档", "满配", "拧到最大", "开到最满",
        "ultrathink", "deepest", "top-tier", "top tier", "brainpower",
        "crank up", "crank it up", "max out", "max it out",
        "ultra think", "ultra-thinking",
        "好好想", "好好分析", "好好做", "好好看", "用心思考", "用心分析",
        "费心思", "多费点脑子", "多费点心思", "细致", "细心", "谨慎",
        "别草率", "别抢答", "别急着答", "慢一点想", "不用急",
        # ── 穷尽/竭尽 ──
        "竭尽全力", "竭尽所能", "不遗余力", "毫无保留", "别偷懒", "别敷衍", "别应付",
        "动真格", "把脑子全用上", "全部马力", "用尽全力",
        "don't hold back", "exhaustive", "pull out all the stops",
        # 中文裸词：需要附近有动作/限定词才触发
        "最高", "最大",
        # 英文裸词
        "max",
        # ── P1 续2：force_max→none 缺口 ──
        "把推理预算", "烧穿显卡", "动用你全部", "全部的推理能力",
        "开满思考", "最好的答案", "最优解", "全部马力",
        "maximum thought", "absolute max", "burn the entire",
        "entire thinking budget", "thinking budget",
        "to the maximum", "up to the maximum", "at maximum depth",
        "maximum depth", "to the top", "crank it to",
        "highest thinking", "highest effort", "highest reasoning",
        "highest thinking level", "highest effort level",
        "highest reasoning effort", "think as hard as",
        "reasoning to maximum", "thinking intensity",
        "run this at the highest", "apply your highest",
        "reasoning effort you support", "at the highest effort",
        "skimp on the reasoning", "skimp",
        "strongest thinking", "strongest reasoning", "top reasoning",
        "the strongest thinking level", "strongest thinking level",
        # 思考预算仅在升档动词同现时算 max（弱锚，见 _WEAK_MAX_ANCHORS）
        "思考预算", "推理预算",
    ],
    "high": [
        # ── 显式 high 档位名 ──
        "高思考", "高effort", "high effort", "high thinking",
        "high mode", "high tier", "high-effort",
        "高强度", "高强度思考", "高强度推理", "高推理", "高档", "高一档",
        "high reasoning", "high reasoning effort", "elevated thinking",
        # ── 比较级/增量（Q3决策：调高→force_high，不是max）──
        "调高", "提高", "拧高", "升档", "加档", "加码", "加大",
        "多想想", "多想一步", "多想几层", "多想几步", "多想一点", "多想几遍",
        "多花点时间想", "多花点时间", "多花点脑力", "多留点时间",
        "再深一点", "更用力", "更认真", "更深一层", "深想一层",
        "提升推理", "加大推理", "提高思考",
        "一步一步", "逐项", "层层递进", "一步步",
        "高一点", "高一些", "再高一点", "调高一点", "思考调高",
        "抬一档", "再抬", "档再抬",
        "想清楚", "想明白", "推敲", "反复推敲",
        "较深", "较深的思考", "深一点的思考",
        "多花点思考", "多花点思考时间",
        "think harder", "think more", "think deeper", "think a bit more",
        "heavier reasoning", "more deeply", "step by step",
        "raise the effort", "bump it up", "turn it up",
        # 裸 raise it 会 FP（"raise it to the board"）；只留带宾语形态。
        "bump the effort", "up a notch", "one notch up",
        "想那么深",
        # ── P1 续：force_high→none 缺口 ──
        "serious thought", "deeper reasoning", "stronger reasoning",
        "more effort", "extra effort", "harder reasoning",
        # strongest/top 族归 max（与 highest* 一致），勿放 high
        "stronger analysis", "deeper analysis", "more thorough",
        "layer by layer", "edge cases", "failure modes",
        "reasoning chain", "reason it out", "weigh the alternatives",
        "considered answer", "first instinct", "real thought",
        "dig into", "go over the logic", "verify the logic",
        "not be hasty", "genuine analysis", "escalate the thinking",
        "raise the reasoning", "turn the reasoning level",
        "reasoning budget", "thinking level up", "thinking tier",
        "one notch", "or two", "fuller answer",
        # as planned → inherit 词表（勿放 high）
        "费点心思", "严肃推理", "开到高", "最强的一档",
        "排一遍", "推理链", "逻辑链", "一层一层", "跳着说",
        "跟上你的思路", "每一步", "走完整", "别跳步",
        "事实核对", "反例", "长期影响", "格外谨慎",
        "bump the thinking", "reasoning level", "think through carefully",
        "take your time", "go over", "walk me through",
        "layer by layer", "in depth", "root cause",
        "high",
        # ── P1 续2：force_high→none 缺口 ──
        "think this one through carefully", "this one through carefully",
        "dig into this properly", "before answering", "deeper analysis here",
        "genuine analysis", "failure properly", "surface symptom",
        "analysis layer", "哪个最靠谱", "可能性都排一遍",
        "这题需要仔细", "一步跳步", "推理清楚之后", "跟着你的思路",
        "值得仔细", "哪种方案", "更稳",
        # highest* 族归 max（M1），勿放 high
    ],
    "low": [
        # ── 显式 low 档位 ──
        "低思考", "简单想", "快速想", "随便想", "别想太多", "不用想太多",
        "省着点", "省钱", "经济模式", "节约模式", "低成本", "低强度", "低档",
        "最低强度", "最低档", "最省", "经济档", "省点思考", "省算力", "省成本", "省电",
        "low effort", "low thinking", "quick thinking",
        "think less", "think quick", "don't overthink", "dont overthink",
        "别过度", "别太复杂", "简单模式", "快速模式", "简单点",
        "快点回答", "简单回答", "简洁回答", "直接回答", "直接说",
        "秒回", "秒答", "快答", "速回", "快问快答", "快准狠",
        "随便答", "随便说说", "随口", "随手", "浅想", "浅思考",
        "轻描淡写", "凭直觉", "糙一点", "瞎说", "走个过场",
        "别啰嗦", "别费劲", "别动脑子", "别深究", "别上强度", "能简则简",
        # ── 长度/输出要求（Q2决策：→force_low + 记 axis=style）──
        "一行字", "一句话", "一个字", "简短", "简洁", "精炼", "精简",
        "越短越好", "别太长", "只要结论", "直接给结论",
        "别废话", "少废话", "开门见山", "别铺垫",
        "结论放第一句", "分析就免了", "不用解释", "跳过推理",
        "一个词", "十个字以内",
        "one-liner", "one sentence", "keep it short", "tl;dr",
        "brief", "concise", "just the answer", "no preamble",
        "quick answer", "short answer", "one line", "one word",
        "keep it light", "budget mode", "shallow",
        "cut to the chase", "verdict first", "skim", "gist",
        # ── 糊弄/随意 ──
        "糊弄", "随便聊聊", "浅浅", "粗暴",
        # ── P1 续：force_low→none 缺口 ──
        "大概", "大概齐", "差不多", "差不多得了", "别较真", "别太较真",
        "磨叽", "抓紧答", "随性", "别绷着", "赶紧说", "应付一下",
        "烧钱", "便宜跑", "大手大脚", "够用就好", "省算力", "简单过",
        "抠搜", "别浪费算力", "压到最低", "别超支", "yes 或 no",
        "只留结果", "砍掉", "最低", "最省", "最快",
        "minimal", "thinking minimal", "off the cuff", "off the top",
        "quick and dirty", "quick reply", "quick one", "burn brain",
        "as short as", "three words", "headline only", "nothing else",
        "zero walkthrough", "body text", "no body", "token spend",
        "thrifty", "quick pass", "keep it breezy", "breezy",
        "plain answer", "no frills", "fastest", "spitball",
        "gimme the short", "short version", "surface level",
        "thinking light", "low profile", "keep thinking",
        "low",
        # ── P1 续2：force_low→none 缺口 ──
        "浅尝辄止", "直觉回答", "省则省", "能省则省", "效率第一",
        "一分钟内", "超时就算了", "nothing fancy", "no fluff",
        "cut the fluff", "give me the result", "just answer",
        "no body text", "rigor needed", "no rigor", "don't burn",
        "brain cycles", "normal推理", "正常推理",
        "lowest effort", "lowest effort please", "lowest thinking",
        "save the compute", "answer briefly", "briefly",
        "keep it rough", "first pass is fine", "cheap mode",
        "single line", "keep it to a single line",
        "no deep analysis needed", "avoid over-deliberating",
        "省着点花", "把预算省着", "省着点",
        "think too hard about it", "think too much about it",
        "don't think too hard", "dont think too hard",
        # ── §7 中文省钱/英文直给 ──
        "经济实惠", "省着用", "大风刮来",
        "straight and fast", "give it to me straight",
        "cheap and fast", "fast and cheap",
        # ── 泛化改写（probe_a_overfit：勿只收金标字面）──
        "划算", "悠着点", "费很贵", "quick version", "give me the quick",
        # ── 残留漏检补：low 风格词（勿收 max 金标词）──
        "light touch", "bare minimum", "bottom line only",
        "three words", "三个词", "大白话", "别整复杂", "别想复杂",
        "just wing it", "answer casually", "don't sweat", "dont sweat",
        "don't think too much", "dont think too much", "简单答下",
        # 「安排上」已删：裸词把「给我安排上一个深度调研课题」误打成 force_low；
        # 金标「省 token 模式，安排上」仍由「省 token」锚定。
        "别费那", "省 token", "省token",
        "skip the rest", "no deep thought",
    ],
}

# 裸词（最高/最大/max/high/low）需要附近有动作词或限定词才触发
# 否则"最大上下文""max函数""high availability"等会被误触发
_BARE_WORDS = {"最高", "最大", "max", "high", "low"}
# 弱 max 锚：须有升档动词，否则丢弃（防「思考预算省着点花」→max）
_WEAK_MAX_WORDS = {"思考预算", "推理预算", "把推理预算"}
_UPSHIFT_VERBS = [
    "拉满", "开满", "开到", "烧穿", "加大", "顶格", "拧到", "全花", "花掉",
    "全部", "最大", "最高", "全力", "竭尽", "用尽", "开到最大",
    "crank", "maximum", "max out", "go full", "burn", "all out",
]
# 动作词：中文 + 英文（P1 修复：原来只有中文，英文裸词必被过滤）
_ACTION_WORDS = [
    "用", "开", "走", "给", "来", "做", "搞", "跑", "想", "思考",
    "拉", "开到", "调到", "设为", "设成", "调成", "提到", "要", "上", "加",
    "选择", "切换", "档", "模式", "就免", "免了", "处理",
    # 注意：不包含"上"（"上下文"会误触发）—— 但上面加了"上"是因为"要上"是常见搭配
    # 英文动作词（P1 补）
    "use", "go", "run", "set", "give", "turn", "crank", "keep", "make",
    "think", "reason", "answer", "put", "switch", "skip", "never",
]
# 英文档位名词（和裸词组合才触发，如 "max thinking" / "high effort"）
_ACTION_EN_RE = re.compile(
    r"\b(?:use|go|run|set|give|turn|crank|keep|make|think|reason|answer|put|switch|skip|never)\b"
    r"|\b(?:thinking|reasoning|effort|mode|tier|depth|brainpower|level|setting)\b",
    re.IGNORECASE,
)

# 否定词：出现在锚点词左侧窗口内 → 该锚点的含义翻转
NEGATIONS = [
    "不要", "别", "不用", "无需", "不必", "不需", "非", "莫", "勿",
    "不使用", "别用", "不用要", "不采用", "不走", "跳过", "免了", "算了",
    "关掉", "关闭", "去掉", "禁用", "禁止", "别上", "别给",
    "don't", "dont", "no", "never", "not", "without", "skip", "avoid",
    "turn off", "disable", "don't use", "do not", "is off",
    "否决", "排除", "搁置", "缓一缓", "先不", "暂不",
]

# 后置否定只认"打发/作罢"类：锚点右侧的通用否定（别/no）常属于下一小句
# 的词汇化下调（"随便说说，别深究"），不能翻转前一锚点。
_DISMISSIVE_RIGHT = [
    "免了", "算了", "不用了", "不需要了", "就免了", "这次免了",
    "no need", "not this time", "skip it", "off the table",
]

# 限定词：出现在锚点词左侧 → 强化该锚点（"只用最高" = force(max)）
RESTRICTIONS = ["只用", "仅用", "只要", "只给", "只开", "只走", "就要", "必须用", "得用", "来个", "给我"]

# 否定作用域窗口（字符数）：锚点词前面多少个字内算否定作用域
# P2：从 12 提到 24——英文 "never go full max" / "don't set thinking to low" 的否定词在更左侧
SCOPE_WINDOW = 24

# 编译正则（锚点词按长度降序，优先匹配长的）
_ANCHOR_RES = {
    tier: sorted([(len(a), a) for a in words], key=lambda x: -x[0])
    for tier, words in ANCHORS.items()
}
_NEG_RES = [re.compile(re.escape(w), re.IGNORECASE) for w in NEGATIONS]
_RESTR_RES = [re.compile(re.escape(w), re.IGNORECASE) for w in RESTRICTIONS]

# 纯 ASCII 锚点要求 ASCII 词边界：
# 不能用 \b——Python 里汉字也是 \w，导致 "别用max" 的 max 前无边界被过滤。
# 用 (?<![A-Za-z0-9])…(?![A-Za-z0-9])：highest 不会切出 high，别用max 能匹配 max。
_ASCII_ANCHOR_RES = {
    tier: [
        (ln, w, re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        ))
        if w.isascii() else (ln, w, None)
        for ln, w in words
    ]
    for tier, words in _ANCHOR_RES.items()
}


def find_anchors(text: str) -> List[Dict]:
    """在文本中找所有锚点词，返回 [{tier, word, start, end}]，按出现顺序。

    英文（纯 ASCII）锚点要求词边界；中文子串匹配（最长优先由排序保证）。
    """
    hits = []
    lower = text.lower()
    for tier, word_list in _ASCII_ANCHOR_RES.items():
        for _, word, en_re in word_list:
            search_word = word.lower()
            if en_re is not None:
                for m in en_re.finditer(text):
                    hits.append({
                        "tier": tier,
                        "word": m.group(0),
                        "start": m.start(),
                        "end": m.end(),
                    })
                continue
            start = 0
            while True:
                idx = lower.find(search_word, start)
                if idx == -1:
                    break
                hits.append({
                    "tier": tier,
                    "word": text[idx:idx + len(word)],
                    "start": idx,
                    "end": idx + len(word),
                })
                start = idx + 1
    # 同位置取更长；部分重叠一律 **leftmost 优先**（不得用更晚起点的更长锚替换）
    # 修：「最高强度思考」被 high 族「高强度思考」@start+1 夺锚 → force_high
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    filtered = []
    for h in hits:
        if filtered and h["start"] < filtered[-1]["end"]:
            # 与上一锚重叠：保留 leftmost（同 start 已按更长优先排序）
            continue
        filtered.append(h)
    return filtered


_DISMISSIVE_RIGHT_RES = [re.compile(re.escape(w), re.IGNORECASE) for w in _DISMISSIVE_RIGHT]


def detect_negation(text: str, anchor_start: int, anchor_end: Optional[int] = None) -> Optional[str]:
    """检查锚点词左侧（或右侧）SCOPE_WINDOW 个字符内是否有否定词。

    左侧：通用否定词窗口。
    右侧：只认打发类（免了/算了）——通用右窗否定多半属于下一小句。
    返回匹配到的否定词或 None。
    """
    # 左侧窗口
    window = text[max(0, anchor_start - SCOPE_WINDOW):anchor_start]
    for neg_re in _NEG_RES:
        m = neg_re.search(window)
        if m:
            if m.end() <= len(window):
                return m.group()
    # 右侧窗口（后置打发否定："深度思考这次免了" / "max档就免了"）
    if anchor_end is not None:
        right = text[anchor_end:anchor_end + SCOPE_WINDOW]
        for neg_re in _DISMISSIVE_RIGHT_RES:
            m = neg_re.search(right)
            if m:
                return m.group()
    return None


# 否定二分法（裁决5 / P2）：
#   否定「程度/努力」短语 → force_low（"不用深度思考" = 省着点）
#   否定「档位名词」     → exclude（"不要用最高" = 排除 max 档）
_DEGREE_WORDS = re.compile(
    r"深度|认真|仔细|费劲|费心|费力|思考|推理| effort| thinking| reasoning|deep|hard|careful",
    re.IGNORECASE,
)
_TIER_NOUNS = re.compile(
    r"最高|最高档|最高思考|max|ultrathink|拉满|火力全开|high|low|高档|低档|一档"
    r"|高思考|高effort|high effort|high thinking|think\s*hard|think\s*deep"
    r"|深度思考模式|深度模式|low effort|low thinking|最低档|最低思考",
    re.IGNORECASE,
)
# 锚点本身即档位名（被否定对象是 mode/tier 标识，不是程度短语）
# 尽量与 ANCHORS 中的模式名对齐；单独手写仅作兜底
_ANCHOR_IS_TIER = re.compile(
    r"最高|最大|拉满|火力全开|满血|顶格|顶满|ultrathink|max|think\s*hard|think\s*deep"
    r"|高思考|高effort|high|low|高档|低档|最低档|经济档|高强度|最强思考|最猛|顶格"
    r"|深度思考|深度模式|深度推理|深思考|深入思考|开满|开到最大"
    r"|full max|highest|lowest|thinking level|effort level|reasoning effort"
    r"|minimal thinking|lazy mode|economy mode|to the top|to the max"
    r"|up to the maximum|at the highest"
    r"|top\s*档|很重的思考|重的思考模式|heavy thinking",
    re.IGNORECASE,
)
# 「别省/别偷懒/别敷衍」= 升档诉求（转换表，不是 exclude 也不是 force_low）
_UPSHIFT_NEG = re.compile(
    r"别省|不要省|不用省|别偷懒|别敷衍|别糊弄|别应付|别摸鱼"
    r"|don't skimp|do not skimp|don't hold back|no skimping|别省.*预算|省着点花",
    re.IGNORECASE,
)

# P4 替代档：exclude 线索 + 显式中间/下调替代 → force(替代)
# 「简单做/随便答」仍走 exclude（冻结回归例），不在此列
# 词形收窄：裸「默认」会 FP（「接口的默认值先不改」）；只认「默认档」等替代档表达。
# 「常规即可」是金标 force_high 正例，须保留小句内常规+补语形态。
_H_SUB_HIGH = re.compile(
    r"正常(?:答|想|来|回答|处理)"
    r"|常规(?:即可|就行|答|想|来|回答|处理)"
    r"|默认档|常规档|标准档|普通档"
    r"|保持不变|照常"
    r"|\bas usual\b|\bnormally\b|keep it normal|keep it standard"
    r"|(?:default|standard) (?:mode|tier|setting|answer|reply|one)",
    re.IGNORECASE,
)
_H_SUB_LOW = re.compile(
    r"一句话|一行字|简短|短答|直接答|直给"
    r"|\bone sentence\b|\bone-liner\b|keep it short|just answer",
    re.IGNORECASE,
)


def negation_to_intent(neg_word: str, anchor_tier: str, text: str, anchor_start: int, anchor_end: int) -> Dict:
    """否定二分法：否定程度→force_low，否定档位→exclude；别省/别偷懒→force_max。

    优先级（裁决 spec §2 R2 / C-3 + 用户拍板 C1）：
      0. 别省/别偷懒/别敷衍 → force_max（升档，不是 exclude/low）
      1. 被否定锚点是档位/模式名 → exclude
      2. 否定低档行为 → force_low
      3. 仅程度短语 → force_low
      4. 默认 → exclude
    """
    anchor_word = text[anchor_start:anchor_end]
    left = text[max(0, anchor_start - SCOPE_WINDOW):anchor_start]
    right = text[anchor_end:anchor_end + SCOPE_WINDOW]
    phrase = left + anchor_word + right
    span = text[max(0, anchor_start - SCOPE_WINDOW):anchor_end]

    def _exclude():
        return {
            "op": "exclude", "tier": anchor_tier,
            "span": span, "polarity": "negate", "neg_word": neg_word,
            "stage": "anchor", "void_reason": None,
        }

    def _force_low():
        return {
            "op": "force", "tier": "low",
            "span": span, "polarity": "negate", "neg_word": neg_word,
            "anchor": anchor_word, "axis": "style",
            "stage": "anchor", "void_reason": None,
        }

    def _force_max():
        return {
            "op": "force", "tier": "max",
            "span": span, "polarity": "negate", "neg_word": neg_word,
            "anchor": anchor_word, "conversion": "upshift",
            "stage": "anchor", "void_reason": None,
        }

    # 0) 升档转换（用户拍板 C1）：别省那点预算 / don't skimp / 别偷懒
    if _UPSHIFT_NEG.search(phrase):
        return _force_max()

    # 1) 档位/模式名被否定 → exclude
    if _ANCHOR_IS_TIER.search(anchor_word) or _TIER_NOUNS.search(phrase):
        return _exclude()

    # 2) 否定低档行为 → force_low
    if anchor_tier == "low":
        return _force_low()

    # 3) 否定程度词 → force_low
    if _DEGREE_WORDS.search(phrase):
        return _force_low()

    # 4) 默认 → exclude
    return _exclude()


def detect_restriction(text: str, anchor_start: int) -> Optional[str]:
    """检查锚点词左侧是否有限定词（只用/仅用等）。"""
    window = text[max(0, anchor_start - SCOPE_WINDOW):anchor_start]
    for restr_re in _RESTR_RES:
        m = restr_re.search(window)
        if m:
            if m.end() <= len(window):
                return m.group()
    return None


# ── 语境闸门（v2 新增）：命中即作废，优先级最高 ──────────────────────────────────────────────────
# v1 只在 spec 里写了这些规则，代码没实现——这是 Phase 0 基线只有 39.3% 的主要原因之一。

# 条件词：档位词落在条件从句（前/后/同句）→ none（含 "use max if …" 后置条件）
_COND_WORDS = re.compile(
    r"(?:如果|要是|假如|若|万一|实在不行|的话|必要时|除非|需要时|遇到[^，。]{0,12}就|当[^，。]{0,16}时[^，。]{0,8}才|当[^，。]{0,16}时)"
    r"|(?:\bif\b|\bunless\b|in case |whenever |as needed )",
    re.IGNORECASE,
)

# 疑问标记：档位词在疑问句中 → none（祈使式疑问除外，Q4）
# 改为**同小句**判定，废除 ±5 字符邻接（「什么时候该用高思考」间距 6 曾漏拦）
_QUESTION_WORDS = re.compile(
    r"[？?]"
    r"|(?:要不要|是不是|能不能|可不可以|该不该|怎么会|为什么|会不会|值不值|有什么区别|怎么判断|怎么选"
    r"|有没有|是否有|用不用|需不需要"
    r"|什么时候|何时|什么是|什么意思|为何|怎样|哪个更|哪种更|怎么决定|如何决定|如何选|怎么用|是啥)"
    r"|\b(?:whether|should|does|can you|could you|how do|how does|why do|why does|which one|when should|when is|what does|what is|how is|what does .* change|is there|are there)\b"
    r"|^\s*(?:is|are|do|does|can|could|should|would|what|which|when|why|how)\b",
    re.IGNORECASE | re.MULTILINE,
)

_CLAUSE_SPLIT_RE = re.compile(r"[，。！？、；;,.!?]|(?:\s{2,})")


def _clause_span(text: str, pos: int) -> tuple:
    """返回 pos 所在小句的 [start, end)。"""
    starts = [0] + [m.end() for m in _CLAUSE_SPLIT_RE.finditer(text)]
    ends = [m.start() for m in _CLAUSE_SPLIT_RE.finditer(text)] + [len(text)]
    for s, e in zip(starts, ends):
        if s <= pos < e or (s <= pos <= e and s < e):
            if s <= pos <= e:
                return s, e
    return 0, len(text)


def _same_clause(text: str, p1: int, p2: int) -> bool:
    s1, e1 = _clause_span(text, p1)
    s2, e2 = _clause_span(text, p2)
    return not (e1 <= s2 or e2 <= s1)


def _clause_spans(text: str) -> List[tuple]:
    """按小句分隔符切出全部 [start, end)（不含分隔符本身）。"""
    spans = []
    last = 0
    for m in _CLAUSE_SPLIT_RE.finditer(text):
        if m.start() > last:
            spans.append((last, m.start()))
        last = m.end()
    if last < len(text):
        spans.append((last, len(text)))
    return spans or [(0, len(text))]


def _adj_clause_region(text: str, anchor_start: int) -> str:
    """exclude 锚点所在小句 + 左右邻接小句（P4 替代档作用域）。"""
    spans = _clause_spans(text)
    idx = 0
    for i, (s, e) in enumerate(spans):
        if s <= anchor_start < e:
            idx = i
            break
    else:
        idx = len(spans) - 1
    lo = spans[max(0, idx - 1)][0]
    hi = spans[min(len(spans) - 1, idx + 1)][1]
    return text[lo:hi]

# 祈使式疑问（Q4 决策：不落入疑问句拦截）——"能不能认真点"语义是"请认真点"
_IMPERATIVE_QUESTION = re.compile(
    r"能不能.{0,6}(?:认真|仔细|深度|好好|想|分析)"
    r"|(?:can you|could you|please|would you).{0,10}(?:think|analy[sz]e|carefully|deeply|hard)",
    re.IGNORECASE,
)

# 引用/转述标记
_QUOTE_WORDS = re.compile(
    r"同事说|听说|文档里?写|文档里?说|上一?轮|刚才|据说|他说|她?让我|你(?:之前|刚才)说"
    r"|(?:says |said |told me |told |someone (?:said|told)|in the docs|config says|earlier|previous turn)",
    re.IGNORECASE,
)

# 元讨论标记（讨论路由系统本身）
_META_WORDS = re.compile(
    r"Laya|路由器|档位机制|怎么判断|规则怎么写|词典|阈值|开关控制|开关|值不值|花多少"
    r"|是什么意思|怎么用|最佳实践|这条分支|用户要求|actually change"
    r"|add a flag|flag to control|whether [^,]{0,40} is on"
    r"|(?:how does|how do you decide|rule of thumb|effort routing)",
    re.IGNORECASE,
)

# 技术名词黑名单（形容词/代码用法，裁决6）
# 双侧检查：锚点前后的「min/max/用法」都算技术语境
_TECH_NOUNS = re.compile(
    r"^\s*(?:上下文|长度|可用性|延迟|堆|函数|变量名|索引|像素|并发|链路|窗口|最大值|最小值|用法"
    r"|\bmin\b|\bmax\b"
    r"|availability|latency|heap|function|index|throughput|max\(\)|Math\.max|max-heap"
    r"|deep learning|深度学习|deep copy|深度优先|high-performance|高性能"
    r"|low\s*/\s*high|high\s*/\s*low)"
    r"|(?:availability|latency|heap|throughput|max\(\)|Math\.max|max-heap"
    r"|deep learning|深度学习|deep copy|深度优先|high-performance|高性能"
    r"|low\s*/\s*high|high\s*/\s*low)",
    re.IGNORECASE,
)

# 代码环境标记（此前定义未接线 —— P0 接入 _context_gate）
_CODE_MARKERS = re.compile(
    r"`[^`]+`|```[\s\S]*?```|\.[a-z]{1,5}\b|--[a-z-]+|=>|::"
    r"|(?:eval|DEBUG|flag|parameter|variable|field|attribute)\b",
    re.IGNORECASE,
)


def _context_gate(text: str) -> Optional[str]:
    """语境闸门：只有当**档位词本身**处于被讨论/被否定的语境时才作废。

    关键修正：不能因为句子里有疑问词/条件词就作废——
    "请认真思考这个问题" 虽然有"问题"但档位词"认真思考"是指令不是被讨论对象。
    """
    # 找到所有锚点词的位置
    anchor_positions = []
    for tier, words in ANCHORS.items():
        for w in words:
            idx = text.lower().find(w.lower())
            if idx >= 0:
                anchor_positions.append((tier, w, idx, idx + len(w)))
    if not anchor_positions:
        return None  # 无档位词，不需要闸门

    # 祈使式疑问豁免（Q4决策）
    is_imperative_q = bool(_IMPERATIVE_QUESTION.search(text))

    # ── 疑问句：疑问词与档位锚点**同小句**即作废（废除 ±5 字符） ──
    if not is_imperative_q:
        qm = _QUESTION_WORDS.search(text)
        if qm:
            for tier, w, a_start, a_end in anchor_positions:
                if _same_clause(text, qm.start(), a_start) or _same_clause(text, qm.start(), a_end - 1):
                    return "question_adjacent"

    # ── 条件句：条件与锚点同句（前/后皆可，含 "use max if …"） ──
    cond = _COND_WORDS.search(text)
    if cond:
        for tier, w, a_start, a_end in anchor_positions:
            # 前置条件（原逻辑）
            if cond.start() < a_start:
                seg = text[cond.end():a_start]
                if "。" not in seg and "." not in seg and "！" not in seg:
                    if _same_clause(text, cond.start(), a_start) or re.fullmatch(r"[\s,;:，、]*|[^。.!！]{0,40}", seg):
                        if re.fullmatch(r"[^。.!！]*", seg) and len(seg) < 80:
                            return "conditional"
                    if _same_clause(text, cond.start(), a_start):
                        return "conditional"
            # 后置条件：锚点在前、if/如果 紧随其后同小句（"use max if the algorithm…"）
            elif cond.start() >= a_end:
                seg = text[a_end:cond.start()]
                if _same_clause(text, a_end - 1, cond.start()) and len(seg) < 40:
                    return "conditional"

    # ── 元讨论：元词与锚点同小句 ──
    meta = _META_WORDS.search(text)
    if meta:
        for tier, w, a_start, a_end in anchor_positions:
            if _same_clause(text, meta.start(), a_start) or abs(meta.start() - a_start) <= 10:
                return "meta_discussion"

    # ── 引用：引用标记在锚点前且同小句 ──
    quote = _QUOTE_WORDS.search(text)
    if quote:
        for tier, w, a_start, a_end in anchor_positions:
            if quote.start() < a_start and _same_clause(text, quote.start(), a_start):
                seg = text[quote.end():a_start]
                if "。" not in seg and "." not in seg:
                    return "quotation"

    # ── 代码环境：反引号/代码块，或 _CODE_MARKERS 命中锚点邻域 ──
    for tier, w, a_start, a_end in anchor_positions:
        if re.search(rf"`[^`]*{re.escape(w)}[^`]*`", text, re.IGNORECASE) or \
           re.search(rf"```[\s\S]*?{re.escape(w)}[\s\S]*?```", text, re.IGNORECASE):
            return "code_context"
        # 接线 _CODE_MARKERS：锚点左右 8 字内有代码记号
        region = text[max(0, a_start - 8):min(len(text), a_end + 8)]
        if _CODE_MARKERS.search(region):
            # 排除普通句号域名误伤：`.` 后必须跟 1-5 字母才算扩展名
            cm = _CODE_MARKERS.search(region)
            if cm and not (cm.group() == "." and not re.search(r"\.[a-z]{1,5}\b", region, re.I)):
                if cm.group() not in (".",):
                    return "code_context"

    # ── 技术名词：锚点**前或后**紧跟技术名词（含 min/max/latency/low-high 用法） ──
    for tier, w, a_start, a_end in anchor_positions:
        after = text[a_end:a_end + 12]
        before = text[max(0, a_start - 12):a_start]
        # 允许 low-latency / low latency / low/high 等连写与间隔
        if _TECH_NOUNS.match(after) or _TECH_NOUNS.match(after.lstrip("-–—/ \t")):
            return "adjective_usage"
        if re.search(r"low\s*/\s*high|high\s*/\s*low", before + w + after, re.I):
            if w.lower() in ("low", "high", "最低", "最高", "低档", "高档"):
                return "adjective_usage"
        # 「min和max的用法」：max 后紧跟 的用法 / 前有 min
        if re.search(r"的用法$|用法", after) or re.search(r"\bmin\b$", before, re.I):
            return "adjective_usage"
        if re.search(r"(?:min|max)\s*(?:和|与|及|,|/)?\s*$", before, re.I) and w.lower() in ("max", "min", "最高", "最大"):
            # 「min和max」中 max 前是 min
            if re.search(r"\bmin\b", before, re.I) or "min" in before.lower():
                return "adjective_usage"

    return None
# 场景一：上一轮失败/卡顿后用户说"继续"，Laya 会判成 low（没内容），但应该保持原档位。
# 场景二：上一轮给出了方案，用户回一句"好的/同意/没问题/按你的来"就是要开始执行。此时任务难度
#         没有任何变化，只是从「讨论」变成「执行」，重判同样会因为消息无内容而掉到 low。
# 匹配规则：文本先按标点切词，**每个词**都必须在词表里才认继承（见 _is_all_inherit_tokens）。
# 这不是保守过头：带内容的句子一定切不完——"继续优化这个函数" 整体不是词，
# "start the server" 里的 the/server 不在词表——所以那些句子继续交给 Laya。
# 好处是逗号连写的批准语也能认："yes, go ahead"、"好的，开始吧"。
INHERIT_WORDS = [
    # 继续 / 下一步（中）
    "继续", "接着", "然后呢", "下一步", "继续吧", "继续来", "继续做",
    "然后", "接下来", "再继续", "接着来",
    "继续推进", "继续搞", "继续弄", "继续整", "继续说", "继续干",
    "接着整", "接着弄", "接着说", "接着推进", "接着做",
    "往下说", "往下走", "往下讲", "往下弄", "往下写", "往下推进", "再往下",
    "走起", "开干", "干起来", "下一步呢",
    # 批准执行（中）：方案已给出，用户点头
    "好的", "好吧", "好", "行", "可以", "同意", "赞成", "没问题", "没毛病",
    "按你的来", "按你说的来", "按你说的办", "按你说的", "按这个来", "按这个办",
    "听你的", "就这么办", "这么办", "就这么定", "就按这个",
    "开始吧", "开始", "执行吧", "干吧", "搞吧", "来吧", "上吧", "动手吧",
    "收到", "照办", "照做", "明白", "明白了", "了解", "了解了", "懂了", "知晓了",
    "好嘞", "好哦", "好呀", "好哒", "好滴", "嗯好", "嗯呢", "嗯呐", "嗯嗯",
    "成", "成吧", "中", "中啊", "中嘞", "妥了", "妥妥的", "准了",
    "行吧", "行嘞", "行行行", "可以可以", "批准了", "去办吧",
    # 继续（英）
    "go on", "continue", "keep going", "carry on", "next", "then what", "go ahead",
    "keep at it", "keep it going", "keep rolling", "keep moving", "keep on going",
    "continue on", "continue please", "onward", "on you go",
    # 批准执行（英）
    "ok", "okay", "yes", "yeah", "yep", "yup", "sure", "sure thing", "yes please",
    "no problem", "no prob", "no promblem", "no worries", "np",
    "agreed", "agree", "approved", "sounds good", "lgtm", "looks good",
    "do it", "go for it", "let's go", "lets go", "proceed", "start",
    "alright", "fine", "cool", "roger", "got it", "understood", "noted",
    "copy that", "that works", "works for me", "that's good", "all good",
    "ship it", "make it so", "let's roll", "green light", "roger that",
    "righto", "mmhm", "sure sure",
    # ── P1 续：inherit→none 缺口 ──
    "保持", "继续保持", "开工", "执行", "推进", "下去", "走", "定",
    "就这样", "就按这个走", "按这个走", "按这个推进", "得嘞", "说下去",
    "继续哈", "没问题的", "可以的", "可以哈", "嗯行", "好嘞，走",
    "就按这个", "按你说的做", "照你说的做", "同意执行", "那就上",
    "那就按你说的来", "按你的思路走", "按刚才说的办", "按这个方案推进",
    "sounds good", "as planned", "please continue", "with that",
    "go ahead with", "from where you left", "left off",
    "do that", "fine by me", "sounds right", "approved",
    "you're good", "good to go", "go right ahead", "make it happen",
    "ship it then", "my go-ahead", "sounds fine", "that's fine",
    "affirmative", "go on then", "go ahead then", "what's next",
    "go on ahead", "let's do it", "go ahead and", "do it then",
    "嗯，可以", "嗯，好的", "好，开工", "行，那就",
    # ── P0：金标继承缺口 ──
    "没意见", "没意见，执行", "照这个办", "就这么干", "没问题，就这么干",
    "接着往下", "就照这个", "照这个", "可以呀", "ok继续",
    "proceed as planned", "carry on then", "ship it", "roger that",
    "that's good", "yes do it", "yes, do it",
    # ── 恢复 P1 续2（勿丢）──
    "嗯", "就按你说的做", "就按你说的", "刚才说的办", "方案权衡听你的",
    "方案权衡听你的，继续", "听你的，继续", "请开始", "批准", "批准执行",
    "approved go", "approved go ahead", "go now", "please go",
    "you're good to go", "you have my go-ahead", "have my go-ahead",
    "sounds good go ahead", "sounds good carry on", "good to go",
    "continue as planned", "continue from where you left off",
    "from where you left off", "where you left off",
    "就按刚才", "按刚才说的", "嗯执行吧", "好的请开始", "执行吧",
    # ── §7/inherit 残留 ──
    "按你的方案来", "按方案来", "好，那就这样", "那就这样", "好那就这样",
    "按你的方案", "你的方案来",
    # ── M3 新增短语回归 ──
    "按原计划执行", "按原计划办",
]

# 分词：英文按单词，中文逐字（P1 修复：原来中文整串一个token，DP切不开）
_TOKEN_RE = re.compile(r"[0-9a-z']+|[\u4e00-\u9fff]")  # P1: 中文单字
_INHERIT_TOKENS = [tuple(_TOKEN_RE.findall(w.lower())) for w in INHERIT_WORDS]


_FILLER_CHARS = set('的了吧呢啊哈哦呀嘞呐滴哒嘛咯呗喽诶欸唉哟嘿了吗着过很太挺请就先呀嘛呗')
_FILLER_EN = {'um', 'uh', 'hm', 'hmm', 'well', 'just', 'then', 'so', 'and', 'or', 'a', 'an', 'the', 'to', 'it', 'that', 'this', 'my', 'from', 'where', 'you'}


def _is_all_inherit_tokens(tokens: List[str]) -> bool:
    """整串 token 能否被词表完整切分（允许跳过语气填充词）。

    用 DP 而不是贪心取长词：贪心在 "sure thing" 这类前缀重合的地方会选错。
    """
    n = len(tokens)
    reach = [False] * (n + 1)
    reach[0] = True
    for i in range(n):
        if reach[i]:
            t = tokens[i]
            if (len(t) == 1 and t in _FILLER_CHARS) or t in _FILLER_EN:
                if not reach[i + 1]:
                    reach[i + 1] = True
            for entry in _INHERIT_TOKENS:
                j = i + len(entry)
                if j <= n and not reach[j] and tuple(tokens[i:j]) == entry:
                    reach[j] = True
    return reach[n]


def parse_intent(text: str) -> Optional[Dict]:
    """解析用户消息中的显式档位意图。

    返回结构化约束 {op, tier, span, polarity, stage} 或 None（无意图）。

    stage 字段标注命中位置（诊断用）：
      gate       — 语境闸门拦截
      inherit    — 继承词命中
      anchor     — 锚点匹配成功
      no_anchor  — 词表里没找到任何锚点
      bare_filtered — 裸词被动作词过滤器丢弃

    op 语义（约束代数）：
      force(t)   — 明确要 t 档（"用最高"、"只用low"）
      exclude(t) — 排除 t 档（"不要用最高"）
      inherit    — 保持上一轮档位（"继续"、"go on"）

    Phase 0 只实现 force、exclude、inherit。
    """
    if not text:
        return {"op": "none", "tier": None, "span": text, "polarity": "neutral",
                "stage": "empty_text", "void_reason": "empty"}

    # ── 语境闸门（v2，优先级最高）──
    gate = _context_gate(text)
    if gate is not None:
        return {"op": "none", "tier": None, "span": text, "polarity": "neutral",
                "stage": "gate", "void_reason": gate}

    # ── 继承意图 ──
    # 长度门槛：去标点+折叠空白后 ≤20 字符；纯 ASCII 且 ≤6 词可放宽
    # （"please continue as planned" 26 字符但 4 词，全是继承词）
    stripped = text.strip()
    stripped_nopunct = re.sub(r"[，。！？、,.!?;：:…~\-]+", " ", stripped)
    stripped_nopunct = re.sub(r"\s+", " ", stripped_nopunct).strip()
    _len_ok = (
        len(stripped_nopunct) <= 20
        or (stripped_nopunct.isascii() and len(stripped_nopunct.split()) <= 6)
    )
    if _len_ok:
        tokens = _TOKEN_RE.findall(stripped_nopunct.lower())
        if tokens and _is_all_inherit_tokens(tokens):
            return {
                "op": "inherit", "tier": None, "span": stripped,
                "polarity": "neutral", "word": " ".join(tokens),
                "stage": "inherit", "void_reason": None,
            }

    # ── 档位锚点匹配 ──
    anchors = find_anchors(text)
    if not anchors:
        return {"op": "none", "tier": None, "span": text, "polarity": "neutral",
                "stage": "no_anchor", "void_reason": "vocabulary_gap"}

    # 裸词 + 弱锚过滤：作用于**全部**锚点（原先只看 anchors[0]）
    def _has_action(window: str) -> bool:
        return any(act in window for act in _ACTION_WORDS) or bool(_ACTION_EN_RE.search(window))

    def _has_upshift(window: str) -> bool:
        return any(v in window for v in _UPSHIFT_VERBS)

    filtered = []
    dropped_bare = False
    dropped_weak = False
    for x in anchors:
        wl = x["word"].lower()
        window = text[max(0, x["start"] - SCOPE_WINDOW):x["end"] + SCOPE_WINDOW]
        if wl in _BARE_WORDS:
            # 否定语境下裸词保留：「无需 max」「别用 low」→ exclude，而非 none
            if not _has_action(window) and detect_negation(text, x["start"], x["end"]) is None:
                dropped_bare = True
                continue
        if x["word"] in _WEAK_MAX_WORDS:
            # 「思考预算省着点花」：有省/节约且无升档动词 → 丢
            if re.search(r"省着|节约|省钱|便宜|够用", window) and not _has_upshift(window):
                dropped_weak = True
                continue
            if not _has_upshift(window) and not _has_action(window):
                dropped_weak = True
                continue
        filtered.append(x)

    if not filtered:
        if dropped_bare:
            return {"op": "none", "tier": None, "span": text, "polarity": "neutral",
                    "stage": "bare_filtered", "void_reason": "no_action_context"}
        if dropped_weak:
            return {"op": "none", "tier": None, "span": text, "polarity": "neutral",
                    "stage": "bare_filtered", "void_reason": "weak_anchor_filtered"}
        return {"op": "none", "tier": None, "span": text, "polarity": "neutral",
                "stage": "no_anchor", "void_reason": "vocabulary_gap"}

    anchors = filtered
    a = anchors[0]
    span_start = max(0, a["start"] - SCOPE_WINDOW)
    span = text[span_start:a["end"]]

    # 检查否定（含后置否定 + 否定二分法）
    neg_word = detect_negation(text, a["start"], a["end"])
    if neg_word:
        intent = negation_to_intent(neg_word, a["tier"], text, a["start"], a["end"])
        # P4：exclude + 显式替代档 → force(替代)；「简单做/随便答」仍 exclude
        # 替代词只在 exclude 锚点所在/邻接小句生效（防跨句「默认值」误替代）
        if intent.get("op") == "exclude":
            _sub_region = _adj_clause_region(text, a["start"])
            if _H_SUB_HIGH.search(_sub_region):
                return {
                    "op": "force", "tier": "high",
                    "span": intent.get("span", span),
                    "polarity": "assert",
                    "anchor": a["word"],
                    "conversion": "substitute",
                    "exclude_slot": intent.get("tier"),
                    "stage": "anchor", "void_reason": None,
                }
            if _H_SUB_LOW.search(_sub_region) and a["tier"] != "low":
                return {
                    "op": "force", "tier": "low",
                    "span": intent.get("span", span),
                    "polarity": "assert",
                    "axis": "style",
                    "anchor": a["word"],
                    "conversion": "substitute",
                    "exclude_slot": intent.get("tier"),
                    "stage": "anchor", "void_reason": None,
                }
        return intent

    # 检查限定（只用/仅用 → force）
    restr_word = detect_restriction(text, a["start"])
    if restr_word:
        return {
            "op": "force",
            "tier": a["tier"],
            "span": span,
            "polarity": "assert",
            "restriction": restr_word,
            "anchor": a["word"],
            "stage": "anchor",
            "void_reason": None,
        }

    # 无否定无限定 → force（默认肯定）
    return {
        "op": "force",
        "tier": a["tier"],
        "span": span,
        "polarity": "assert",
        "anchor": a["word"],
        "stage": "anchor",
        "void_reason": None,
    }


def apply_constraint(base_tier: str, intent: Optional[Dict], prev_tier: Optional[str] = None) -> str:
    """将意图约束应用到内容判断的 base_tier 上，返回最终档位。

    约束代数：
      force(t)   → 直接返回 t（用户显式指定，最高优先）
      exclude(t) → 如果 base_tier == t，降一档；否则不变
      inherit    → 返回 prev_tier（保持上一轮档位）；无上一轮则回退 base_tier
      none       → 返回 base_tier（无意图；显式契约，不靠「未知 op」落网）
      None       → 返回 base_tier（无意图，内容判断说了算）

    降档规则（exclude 的处理）：
      exclude(max) 且 base=max → high（降一档，不降到底）
      exclude(high) 且 base=high → low
      exclude(low) 且 base=low → high（反向：排除 low = 至少 high）
    """
    if intent is None:
        return base_tier

    tier_order = ["low", "high", "max"]

    # P0 后 parse_intent 对 empty/gate/no_anchor 也返回 dict op=none；内容判断说了算。
    if intent["op"] == "none":
        return base_tier

    if intent["op"] == "force":
        # 用户显式指定，一票否决
        return intent["tier"]

    if intent["op"] == "inherit":
        # 保持上一轮档位；没有上一轮则用 Laya 的判断
        if prev_tier is not None:
            return prev_tier
        return base_tier

    if intent["op"] == "exclude":
        target = intent["tier"]
        if base_tier == target:
            if target == "max":
                return "high"      # 排除 max → 降一档
            if target == "high":
                return "low"       # 排除 high → 降一档
            if target == "low":
                return "high"      # 排除 low → 升一档（"别用low"= 至少 high）
        # base_tier 不是被排除的档 → 不影响
        return base_tier

    # 未知 op（Phase 1 扩展）
    return base_tier
