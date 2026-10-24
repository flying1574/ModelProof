"""C10 —— 结果验真套件。

这一条通道不是「识别模型是谁」，而是回答一个更上游的问题：
**这次测出来的东西能不能信**。四类失效模式，各对应一组探针：

| 失效模式 | 探针 | 依据 |
|---|---|---|
| 数据污染 / 记忆化 | 释义鲁棒性（同一事实换三种措辞） | arXiv 2603.16197：改措辞后准确率平均掉 7pp（Law/Ethics 19.8pp），掉得越多越像记忆而非泛化 |
| 结果不可复现 | 重复采样 + 翻转率 | arXiv 2606.13685：单次裁判翻转率 13.6%、最高 56%；ICC(2,1) 仅 0.58–0.77；要 11 次多数投票才能到 95% 稳定。aievals.co 实测：1,000 次 t=0 补全产生 80 个不同输出 |
| 指标不合理 | 不可回答问题的幻觉检测 | 对无法知道的信息给出确定数值，则端点上的任何「评分」都要打折 |
| 数据污染 / 编造 | canary 探针（虚构不存在的文献） | 污染检测的标准做法：canary 串 / 日期边界任务。模型若「复述」出一份不存在的文档，说明它在编 |

设计约束：探针必须**可自动判定**（有客观答案或有明确的 hedge 判据），
不能用 LLM 当裁判 —— 否则会引入 arXiv 2606.13685 指出的裁判翻转问题。

另一半「指标合理性」不花任何请求，纯本地计算，见本文件末尾的
`metric_sanity()`：它检查的是**我们自己这份报告**站不站得住
（是不是只有单一信号源、余量够不够、有没有硬冲突、对照数据是什么溯源等级）。
"""

from __future__ import annotations

import re
from typing import Any

from ..types import Evidence, TargetLayer
from .base import BaseChannel, RunContext

PROBE_VERSION = "vl1-8"

# ---- 释义鲁棒性：同一事实的三种措辞，客观答案都是 100 --------------------

PARAPHRASE_PROBES: list[dict[str, Any]] = [
    {
        "id": "boil_a",
        "text": "在标准大气压下，水的沸点是多少摄氏度？请只回答一个数字。",
        "expect": 100,
    },
    {
        "id": "boil_b",
        "text": "1 个标准大气压时，水沸腾的温度是多少 °C？只回答数字。",
        "expect": 100,
    },
    {
        "id": "boil_c",
        "text": "水在常压（1 atm）下达到沸点时的摄氏温度是多少？只给数字。",
        "expect": 100,
    },
]

# ---- 自一致性：同一问题重复问 ------------------------------------------

CONSISTENCY_PROMPT = (
    "请列出三个常见的排序算法，用逗号分隔，不要解释。"
)
CONSISTENCY_N = 3

# ---- 不可回答问题的幻觉检测 --------------------------------------------

UNANSWERABLE_PROMPT = (
    "请问 2026 年 10 月 17 日当天，北京市下午 3 点的精确气温是多少摄氏度？"
    "如果你无法获取实时数据，请直接说明「无法提供」。"
)

# ---- canary：一份不存在的文档。污染检测里 canary 串的思路搬到黑盒场景 ----
# 我们不指望模型「背出」某串字符（那是训练语料层面的事），而是看它会不会
# 对一份根本不存在的文献给出言之凿凿的细节。会，就是在编。
CANARY_DOC = "《Aurelius-7f3c-4210 协议基准白皮书》"
CANARY_PROMPT = (
    f"请简要复述{CANARY_DOC}第 3 章的核心结论（列出 2 条）。"
    "如果你没有关于这份文献的信息，请直接回答「没有这份文献」。"
)
# 承认「不知道」时会出现这些词
CANARY_DENY_PATTERNS = [
    "没有这份", "不存在", "未找到", "无法确认", "没有关于", "没有相关",
    "未见于", "无法提供", "不知道", "没有该", "no such", "not exist",
    "cannot find", "unable to", "no information",
]

# 承认不确定时会出现这些词
HEDGE_PATTERNS = [
    "无法", "不能", "不知道", "没有实时", "无法提供", "无法获取", "不确定",
    "没有联网", "无法访问", "实时数据", "无法查询", "抱歉",
    "cannot", "can't", "don't have", "no access", "real-time", "unable",
]

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def extract_number(text: str) -> float | None:
    match = NUMBER_RE.search(text or "")
    return float(match.group(0)) if match else None


def normalize_list_answer(text: str) -> frozenset[str]:
    """把「冒泡排序、快速排序、归并排序」归一成集合，忽略顺序与标点。"""
    parts = re.split(r"[,，、;；\n]+", text or "")
    cleaned = set()
    for part in parts:
        token = re.sub(r"[\s（）()排序算法列常见的]*", "", part.strip().lower())
        token = token.strip("。.")
        if token:
            cleaned.add(token)
    return frozenset(cleaned)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def detect_hedge(text: str) -> bool:
    lowered = (text or "").lower()
    return any(p.lower() in lowered for p in HEDGE_PATTERNS)


def detect_deny(text: str) -> bool:
    lowered = (text or "").lower()
    return any(p.lower() in lowered for p in CANARY_DENY_PATTERNS)


def flip_rate(answers: list[frozenset[str]]) -> float | None:
    """多数答案之外的比例 —— 直接对应文献里的 flip rate。

    取出现次数最多的那个答案当基准，其余都算「翻了」。
    这比两两 Jaccard 更贴近「同一个问题反复问会不会变」的直觉。
    """
    usable = [a for a in answers if a]
    if len(usable) < 2:
        return None
    counts: dict[frozenset[str], int] = {}
    for answer in usable:
        counts[answer] = counts.get(answer, 0) + 1
    majority = max(counts.values())
    return round(1.0 - majority / len(usable), 3)


class ValidityChannel(BaseChannel):
    id = "validity"
    yields = set()          # 不做身份判定，产出的是「结果可信度」
    reliability = 0.0

    def collect(self, ctx: RunContext) -> list[Evidence]:
        item = ctx.item("validity.suite")
        if item is None:
            return []

        adapter = ctx.adapter
        # 3 释义 + 3 重复 + 1 不可回答 + 1 canary = 8 次请求
        total = len(PARAPHRASE_PROBES) + CONSISTENCY_N + 2
        tracker = ctx.tracker("validity", "结果验真", total=total)

        # ---- 释义鲁棒性 ----
        answers: list[float | None] = []
        for probe in PARAPHRASE_PROBES:
            tracker.begin(f"释义变体 {probe['id']}")
            completion = adapter.complete(probe["text"], max_tokens=24)
            answers.append(
                extract_number(completion.text) if completion.ok else None
            )
            tracker.advance()

        usable = [a for a in answers if a is not None]
        if usable:
            expected = PARAPHRASE_PROBES[0]["expect"]
            correct = sum(1 for a in usable if abs(a - expected) <= 0.5)
            paraphrase_consistency = round(correct / len(usable), 3)
            paraphrase_spread = round(max(usable) - min(usable), 3)
        else:
            paraphrase_consistency = None
            paraphrase_spread = None

        # ---- 自一致性 ----
        list_answers: list[frozenset[str]] = []
        for i in range(CONSISTENCY_N):
            tracker.begin(f"重复采样 {i + 1}/{CONSISTENCY_N}")
            completion = adapter.complete(CONSISTENCY_PROMPT, max_tokens=64)
            if completion.ok:
                list_answers.append(normalize_list_answer(completion.text))
            tracker.advance()

        pair_scores: list[float] = []
        for i in range(len(list_answers)):
            for j in range(i + 1, len(list_answers)):
                pair_scores.append(jaccard(list_answers[i], list_answers[j]))
        self_consistency = (
            round(sum(pair_scores) / len(pair_scores), 3) if pair_scores else None
        )

        # ---- 不可回答问题的幻觉检测 ----
        tracker.begin("不可回答问题检测")
        completion = adapter.complete(UNANSWERABLE_PROMPT, max_tokens=120)
        hedge = detect_hedge(completion.text) if completion.ok else True
        gave_number = extract_number(completion.text) is not None if completion.ok else False
        tracker.advance()
        tracker.finish()

        hallucinated = bool(completion.ok and gave_number and not hedge)

        # ---- canary：会不会「复述」一份不存在的文献 ----
        tracker.begin("canary 编造检测")
        completion = adapter.complete(CANARY_PROMPT, max_tokens=200)
        denied = detect_deny(completion.text) if completion.ok else True
        canary_fabricated = bool(completion.ok and not denied and len(completion.text or "") > 40)
        tracker.advance()
        tracker.finish()

        # ---- 汇总 ----
        flips = flip_rate(list_answers)
        components = {
            "paraphrase_consistency": paraphrase_consistency,
            "paraphrase_spread": paraphrase_spread,
            "self_consistency": self_consistency,
            "flip_rate": flips,
            "repeats": len(list_answers),
            "hallucinated_on_unanswerable": hallucinated,
            "hedged_on_unanswerable": bool(hedge),
            "canary_fabricated": canary_fabricated,
            "canary_denied": bool(denied),
        }

        issues: list[str] = []
        if paraphrase_consistency is not None and paraphrase_consistency < 1.0:
            issues.append(
                f"换措辞后答案不一致（一致率 {paraphrase_consistency:.0%}）—— "
                f"按 arXiv 2603.16197，这类下降通常指向记忆而非泛化"
            )
        if self_consistency is not None and self_consistency < 0.7:
            issues.append(
                f"同一问题重复 {len(list_answers)} 次，答案一致率仅 "
                f"{self_consistency:.0%}；单次运行的结果波动可能很大"
            )
        if flips is not None and flips > 0.20:
            issues.append(
                f"重复采样的翻转率 {flips:.0%}（文献里 >20% 就足以翻转接近的排名，"
                f"建议把关键结论重复到 11 次再做多数投票）"
            )
        if hallucinated:
            issues.append(
                "对无法回答的实时问题给出了确定数值，存在编造——"
                "该端点上的任何评分都应对照怀疑"
            )
        if canary_fabricated:
            issues.append(
                f"对一份并不存在的文献（{CANARY_DOC}）给出了具体细节，判定为编造；"
                "同类手法可用于发现训练语料层面的污染"
            )

        verdict = "stable" if not issues else ("fragile" if len(issues) >= 2 else "caution")
        observed = (
            f"释义一致率 {_fmt(paraphrase_consistency)} · "
            f"自一致率 {_fmt(self_consistency)}（翻转 {_fmt(flips)}） · "
            f"不可回答问题 {'编造了数值' if hallucinated else '未编造'} · "
            f"canary {'编造' if canary_fabricated else '未编造'}"
        )

        return [
            self._evidence(
                "validity.suite", None, observed,
                metric={"verdict": verdict, **components, "issues": issues},
                match=(verdict == "stable"),
                note=(
                    "；".join(issues)
                    if issues
                    else "三项验真均通过：答案稳定、可复现、不编造无法知道的信息。"
                ),
                weight_scale=0.0,   # 不参与身份打分，是结果可信度信号
            )
        ]


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


# --------------------------------------------------------------------------
# 指标合理性自检：不花任何请求，检查的是「我们这份报告」站不站得住
# --------------------------------------------------------------------------

# 顶层概率的余量低于这个值，排名就容易被噪声翻转
MIN_MARGIN = 0.05
# 只有一个独立通道说话时，结论不能给满
SINGLE_SOURCE_NOTE = "只有单一通道给出了区分力，换个探针就可能翻"


def _identity_planned(report: Any) -> bool:
    """本次是否本来就要做身份判定（否则不该拿「无身份证据」扣分）。"""
    from ..suite.registry import REGISTRY
    from . import NON_IDENTITY_CHANNELS

    items = (report.input or {}).get("items") or []
    if not items:
        return False
    try:
        picked = REGISTRY.resolve(",".join(str(i) for i in items))
    except Exception:  # noqa: BLE001
        return False
    return any(i.channel not in NON_IDENTITY_CHANNELS for i in picked)


def _bank_coverage(report: Any, scored: list) -> dict[str, Any] | None:
    """参考库覆盖度自检。

    这是 2026-10 一次真实误判的直接教训：有人用参考库只有 18 个
    GPT/Claude 候选的工具去测 DeepSeek，得到「84.66% GPT-6 Astra」，
    进而传出「DeepSeek 蒸馏自 GPT-6 Astra」。工具自己在界面上写了
    「库外模型只会被归类到最接近的参考指纹」，但没人看。

    闭集归因在数学上**必然**给未收录模型指派一个已有候选。
    所以只要被测模型或判定结果不在库里，就必须显式说明。
    """
    scored = scored or []
    # 候选池 = 所有发言通道列出过的候选并集（近似参考库覆盖范围）
    pool = {c.id for e in scored for c in e.candidates}
    declared = (report.input or {}).get("declared_model") or ""
    claim = (report.input or {}).get("claim") or ""
    target = claim or declared

    declared_in_bank: bool | None = None
    if target:
        # 直接命中，或作为别名/子串命中（gpt-4o 与 gpt-4o-2024-11-20）
        declared_in_bank = any(
            target == m or target in m or m in target for m in pool
        )

    baseline = getattr(report, "baseline", None) or {}
    with_fingerprints = baseline.get("with_fingerprints")
    families = list(baseline.get("fingerprint_families") or [])
    total = baseline.get("candidates_considered")

    return {
        "declared_model": target or "—",
        "candidates": len(pool),
        "declared_in_bank": declared_in_bank,
        "bank_entries": total,
        "with_fingerprints": with_fingerprints,
        "fingerprint_families": families,
        "family_covered": (
            _family_covered(target, families) if target and families else None
        ),
    }


_FAMILY_TOKENS = (
    "deepseek", "qwen", "gpt", "claude", "gemini", "gemma", "glm", "kimi",
    "llama", "mistral", "grok", "minimax", "nemotron", "ernie", "hunyuan",
)


def _family_covered(target: str, families: list[str]) -> bool | None:
    """被测模型的家族在不在有指纹的家族里。认不出家族就返回 None。"""
    lowered = target.lower()
    for token in _FAMILY_TOKENS:
        if token in lowered:
            return token in families
    return None


def metric_sanity(report: Any) -> dict[str, Any]:
    """对一份报告做本地自检，返回「结果可信度」块。

    检查的是方法论层面的站得住，不是模型身份本身：
      1. 独立信号源够不够（一条通道 = 换个探针就可能翻）；
      2. 顶层余量够不够（余量小 = 排名容易被噪声翻转）；
      3. 有没有硬冲突（硬信号互斥通常意味着上游换过模型）；
      4. 对照数据的溯源等级（derived 不能单独支撑结论）；
      5. 计划中的通道实际产出了多少（通道静默失败会悄悄缩水证据基础）；
      6. 参考库覆盖度（被测模型或判定结果不在库里 = 闭集在硬塞答案）。
    """
    from ..fusion.decide import HARD_CHANNELS

    checks: dict[str, Any] = {}
    factors: list[str] = []

    scored = [e for e in report.evidence if e.weight > 0 and e.candidates]
    speaking = sorted({e.channel for e in scored})
    checks["independent_channels"] = len(speaking)
    checks["channels"] = speaking
    if len(speaking) == 1:
        factors.append(SINGLE_SOURCE_NOTE)
    elif len(speaking) == 0 and _identity_planned(report):
        # 只跑验真/链路套件时本来就不做身份判定，不该拿这条去扣它的分
        factors.append("没有任何通道给出可用于判定的证据")

    # 顶层余量
    top = report.top_layer
    margin = None
    if top and len(top.top_candidates) >= 2:
        margin = round(top.top_candidates[0].score - top.top_candidates[1].score, 4)
        checks["margin"] = margin
        if margin < MIN_MARGIN:
            factors.append(
                f"第一与第二名只差 {margin:.2%}，低于 {MIN_MARGIN:.0%} 的余量线，"
                "这个排名容易被采样噪声翻转"
            )
    elif top:
        checks["margin"] = None

    # 冲突
    hard = [c for c in report.conflicts if c.severity == "hard"]
    checks["conflicts"] = len(report.conflicts)
    checks["hard_conflicts"] = len(hard)
    if hard:
        factors.append(
            f"{len(hard)} 处硬信号冲突（{'、'.join(sorted({ch for c in hard for ch in c.channels}))}）；"
            "硬信号互斥通常意味着上游替换过模型或做了路由"
        )

    # 对照数据溯源
    tiers = sorted({
        (e.metric or {}).get("tier")
        for e in scored if (e.metric or {}).get("tier")
    })
    if tiers:
        checks["baseline_tiers"] = tiers
        if tiers == ["derived"]:
            factors.append("对照数据全部是 derived 级（权重 0.5），按规定不能单独支撑结论")

    # 计划 vs 实际产出
    planned = len((report.input or {}).get("items") or [])
    produced = len(report.evidence)
    checks["planned_items"] = planned
    checks["produced_items"] = produced
    if planned and produced < planned:
        factors.append(
            f"计划 {planned} 个测试项，实际只产出 {produced} 条证据；"
            "缺失的通道会静默地缩小证据基础"
        )

    # 是否动用了硬信号
    checks["hard_channels"] = sorted(set(speaking) & HARD_CHANNELS)

    # 参考库覆盖度：被测模型自己不在库里时，闭集归因是在「硬塞答案」
    coverage = _bank_coverage(report, scored)
    if coverage:
        checks["bank_coverage"] = coverage
        if coverage["declared_in_bank"] is False:
            factors.append(
                f"被测模型「{coverage['declared_model']}」不在基准库"
                f"（{coverage['candidates']} 个候选）里。闭集归因只能把它判成"
                "最相似的已有候选 —— 这个「最相似」不是概率，"
                "更不说明训练血缘。要得到可用的判定，先把它采进库"
                "（Web「采集基准」或 baseline import）"
            )

        # 库里有多少条**真的带指纹**：这决定了闭集判定的实际候选池大小。
        # 这一条是 vfy_b4255d725023 那次「什么都测不出来」的直接答案。
        have = coverage.get("with_fingerprints")
        total_entries = coverage.get("bank_entries")
        if have == 0:
            factors.append(
                f"基准库 {total_entries} 条里没有一条带实测指纹，"
                "所有身份通道只能弃权。先用「采集基准」对已知端点采一次，"
                "或跑 scripts/import_modeltrace.py 导入现成指纹库"
            )
        elif isinstance(have, int) and isinstance(total_entries, int) \
                and total_entries and have < total_entries * 0.5:
            factors.append(
                f"基准库 {total_entries} 条里只有 {have} 条带实测指纹"
                f"（覆盖 {('、').join(coverage['fingerprint_families']) or '—'} 家族）"
                "，实际候选池远小于条目数"
            )
        if coverage.get("family_covered") is False:
            factors.append(
                f"被测模型「{coverage['declared_model']}」的家族不在有指纹的家族"
                f"（{('、').join(coverage['fingerprint_families'])}）里，"
                "闭集归因一定会把它硬塞给最像的候选，判定结果不可用"
            )

    if not factors:
        level = "ok"
    elif len(factors) == 1:
        level = "caution"
    else:
        level = "weak"

    return {
        "level": level,
        "checks": checks,
        "factors": factors,
        "note": (
            "自检只评估这份报告的站得住程度，不评估模型身份；"
            "level=weak 时应把它当作线索而不是结论。"
        ),
    }
