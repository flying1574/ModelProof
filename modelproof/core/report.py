"""VerificationReport —— 三入口共用的结果结构。

CLI 把它渲染成表格 / Markdown，Web 把它画成报告页，REST API 直接返回。
任何一次运行都能导出成 report.json，再被任一入口读回来 —— 这是
「结果互通」唯一的实现方式，所以这个结构是硬契约，改动要升 schema_version。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import (
    Conflict,
    Cost,
    CostRange,
    Evidence,
    LAYER_ORDER,
    LayerVerdict,
    TargetLayer,
    Truthfulness,
)

SCHEMA_VERSION = "1.0"

DISCLAIMER = "统计指纹存在误报，结果仅供参考，不得作为法律、纪律或违约认定的依据。"

# 通道 ID → 中文名。报告给人看，不把内部 ID 直接甩出去。
# 常量在 core/channel_meta.py 统一管理（orchestrator 与这里都要用，
# 两边互相 import 会成环，所以抽到不带 import 的模块里）。
from .channel_meta import CHANNEL_LABEL  # noqa: E402

# 线索文本 → 家族。只用来做「两条通道是否同向」的粗匹配，不参与判定。
_LEAD_FAMILY_HINTS: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "anthropic", "sonnet", "opus", "haiku"),
    "gpt": ("gpt", "chatgpt", "openai"),
    "gemini": ("gemini", "gemma", "google"),
    "qwen": ("qwen", "通义", "alibaba", "tongyi"),
    "deepseek": ("deepseek", "深度求索"),
    "kimi": ("kimi", "moonshot"),
    "glm": ("glm", "zhipu", "智谱"),
    "llama": ("llama", "meta"),
    "mistral": ("mistral", "codestral"),
    "grok": ("grok", "xai"),
    "minimax": ("minimax",),
}

# 按「蒸馏能不能把这个信号搬过去」分组。这是两类完全不同的观测面：
#   * 抗蒸馏组：词表是训练时冻结的产物、后端配置由部署方决定、
#     调用链时序由推理栈决定。蒸馏学生模型会继承教师的**输出分布**，
#     但不会顺便继承教师的词表和推理栈。
#   * 可迁移组：答案分布、排版风格、行为口径、能力台阶，
#     这些正是蒸馏想要搬走的东西。
# 两组意见分歧时，本身就是信息 —— 见 lineage_note。
DISTILLATION_RESISTANT = ("tokenizer", "backend_fp", "trace")
DISTILLATION_TRANSFERABLE = (
    "onetoken", "distribution", "style", "behavior", "capability",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id() -> str:
    """可读的短 ID：run_ + 时间戳 + 随机后缀。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6]
    return f"run_{stamp}{rand}"


@dataclass
class VerificationReport:
    schema_version: str = SCHEMA_VERSION
    run_id: str = field(default_factory=_new_run_id)
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None

    input: dict[str, Any] = field(default_factory=dict)
    protocol: dict[str, Any] = field(default_factory=dict)

    layers: dict[TargetLayer, LayerVerdict] = field(default_factory=dict)
    truthfulness: Truthfulness | None = None
    evidence: list[Evidence] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    cost: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    gateway: dict[str, Any] | None = None
    # 结果可信度：C10 的观测 + 本地指标自检。它回答「这份报告能不能信」，
    # 与 layers 回答的「模型是谁」是两件事，不能混在一起。
    validity: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def finish(self) -> "VerificationReport":
        self.finished_at = _now()
        return self

    # -- 便捷访问 ---------------------------------------------------------

    @property
    def top_layer(self) -> LayerVerdict | None:
        """最高一层给出了非空结论的层。"""
        best: LayerVerdict | None = None
        for layer in LAYER_ORDER:
            verdict = self.layers.get(layer)
            if verdict and not verdict.abstained and verdict.value:
                best = verdict
        return best

    @property
    def headline(self) -> str:
        verdict = self.top_layer
        if verdict is None:
            return "无法判定（已弃权）"
        label = verdict.value or "—"
        return f"{label}（置信度 {verdict.confidence:.0%}）"

    @property
    def verdict(self) -> dict[str, Any]:
        """顶层三态裁决 —— 给人和给 CI 看的那一个结论。

        四层判定各有各的置信度，但大多数场景只需要一句话：
        **跟声明对得上、对不上、还是证据不够**。这里把 layers 与
        truthfulness 压成这一个三态，理由一并列出来。

        绝不用「真 / 假」二值：统计指纹必然有误报，
        INCONCLUSIVE 是有效结论，不是失败。
        """
        truth = self.truthfulness
        layers = [
            self.layers.get(layer) for layer in ("version", "series", "family", "vendor")
        ]
        resolved = [v for v in layers if v and not v.abstained and v.value]

        # 真伪模式：有明确声明时，它比身份判定更直接
        if truth and truth.verdict in ("inconsistent", "suspicious"):
            return {
                "state": "MISMATCH",
                "basis": f"真伪核验：{truth.verdict}",
                "reasons": [truth.note or "与声明字段不一致"]
                + [c for c in (f"冲突：{' vs '.join(c.values)}" for c in self.conflicts)],
                "exit_code": 3,
            }

        if not resolved:
            reasons = [
                f"{v.abstain_reason}" for v in layers if v and v.abstained
            ] or ["未产生任何层判定"]
            basis = "所有层均弃权"
            # 有一种弃权是**答案**，不是失败：请求被分派到了多个后端时，
            # 本来就没有任何单一身份可以鉴定。这时候要说清楚，
            # 否则用户只会看到「测不出来」，以为工具坏了。
            mixed = self.multi_backend
            if mixed:
                basis = "请求被分派到多个后端，不存在单一身份"
                reasons.insert(
                    0,
                    "词表探针命中多个不同词表（"
                    + "、".join(
                        f"{family} {n} 格"
                        for family, n in list(mixed["votes"].items())[:4]
                    )
                    + f"），整体相对差 {mixed['best_diff']:.1%} 超出容差，"
                    f"但 {mixed['considered'] - mixed['unresolved']}/"
                    f"{mixed['considered']} 格都能被某个已知词表解释 —— "
                    "这不是「库里没有」，是**同一批探针落到了不同后端**"
                )
            return {
                "state": "INCONCLUSIVE",
                "basis": basis,
                "reasons": reasons,
                "exit_code": 2,
            }

        top = resolved[0]
        margin = None
        if len(top.top_candidates) >= 2:
            margin = round(
                top.top_candidates[0].score - top.top_candidates[1].score, 4
            )

        if truth and truth.verdict == "consistent":
            basis = f"身份判定 {top.value}（{top.confidence:.0%}）且真伪核验一致"
        else:
            basis = f"身份判定 {top.value}（{top.confidence:.0%}）"

        reasons = [f"顶层判定 {top.value}，置信度 {top.confidence:.0%}"]
        if margin is not None:
            reasons.append(f"与第二名差距 {margin:.0%}")
        if self.conflicts:
            reasons.append(
                f"存在 {len(self.conflicts)} 处通道冲突，置信度已打折"
            )
        if self.validity and self.validity.get("level") == "weak":
            reasons.append("结果可信度自检为 weak，建议当作线索而非结论")
        note = self.lineage_note
        if note:
            reasons.append(
                f"软信号与抗蒸馏信号指向不同候选（{'、'.join(note['candidates'])}），"
                "相似性可能来自蒸馏而非同一服务，详见「血缘提示」"
            )
        return {
            "state": "MATCH",
            "basis": basis,
            "reasons": reasons,
            "exit_code": 0,
        }

    @property
    def multi_backend(self) -> dict[str, Any] | None:
        """词表通道是否检测到「请求被分派到多个后端」。

        这是少数几种「弃权本身就是答案」的情形：中转商按请求分派时，
        37 格探针会命中不同词表，任何一个单一模型都说不通。
        此时与其猜一个身份，不如把这件事讲清楚。
        """
        for ev in self.evidence:
            if ev.channel != "tokenizer":
                continue
            block = (ev.metric or {}).get("mixture")
            if isinstance(block, dict) and block.get("mixed"):
                return block
        return None

    @property
    def leads(self) -> dict[str, Any] | None:
        """弃权时的「未确认线索」。

        为什么需要它：一份四层全空的报告对使用者没有任何价值，尤其是明明
        有几条观测已经指向同一个方向的时候。2026-10-18 那个端点是典型例子 ——
        behavior 自述 identity=qwen、JSON 自述 Qwen、tokenizer 最接近
        qwen2-5（虽然超了容差），但报告只写「证据不足」，人还得自己去翻
        证据表才能看出端倪。

        纪律：这些**不是判定**。自述可以伪造，超容差的词表匹配是「不像任何
        已知词表」而不是「像它」。所以每一条都带 strength=weak，头部写明
        「不能作为结论」，并且只在真的弃权时才出现 —— 有判定的时候不摆出来
        添乱。
        """
        speaking = [e for e in self.evidence if e.weight > 0 and e.candidates]
        if speaking:
            return None  # 有通道能判定，不需要线索

        items: list[dict[str, Any]] = []

        # 1) 自述身份。软得不能再软，但它是唯一不需要对照组就能读的东西。
        for ev in self.evidence:
            if ev.channel != "behavior":
                continue
            vector = (ev.metric or {}).get("vector") or {}
            identity = vector.get("identity")
            if identity:
                items.append({
                    "channel": "behavior",
                    "text": f"模型自述身份为「{identity}」",
                    "strength": "weak",
                    "caveat": "自述可被上游 system prompt 改写，是最软的一类信号",
                })
            self_json = vector.get("json_self") or {}
            if isinstance(self_json, dict) and self_json.get("name"):
                items.append({
                    "channel": "behavior",
                    "text": (
                        f"JSON 结构化自述为 {self_json.get('name')}"
                        + (f" / {self_json.get('vendor')}" if self_json.get("vendor") else "")
                    ),
                    "strength": "weak",
                    "caveat": "同上；且与上面的自述可能并不一致",
                })

        # 2) 词表最接近的参考。超容差时它不是匹配，但「最接近谁」仍是有用的方向。
        for ev in self.evidence:
            if ev.channel != "tokenizer":
                continue
            best = (ev.metric or {}).get("vocab_best")
            if not best:
                continue
            within = best.get("diff") is not None and best["diff"] <= 0.06
            items.append({
                "channel": "tokenizer",
                "text": (
                    f"词表最接近本地参考「{best.get('label')}」"
                    f"（{best.get('family')} 家族），相对差 {best.get('diff'):.1%}"
                    f"，{best.get('same_cells')} 格相同"
                ),
                "strength": "match" if within else "weak",
                "caveat": (
                    "在 6% 容差内，可支持家族判断"
                    if within else
                    "**超出 6% 容差，不构成匹配** —— 只能说明它不像任何已知词表，"
                    "可能是库的更新代次没收录"
                ),
            })

        # 3) 闭集内最像的候选。这是最容易被漏掉又最关键的一条：
        #    robust 打分包判库外时会刻意不给候选，但「最像谁、差多少」
        #    本身就是下一步该采什么的方向。
        for ev in self.evidence:
            if ev.channel != "distribution":
                continue
            robust = (ev.metric or {}).get("robust") or {}
            ranked = robust.get("top") or []
            if not ranked:
                continue
            top = ranked[0]
            best = robust.get("best_score")
            threshold = robust.get("out_of_scope_threshold")
            head = f"闭集内最像 {top.get('model')}"
            if best is not None and threshold is not None:
                head += f"（得分 {best:.2f} / 库外阈值 {threshold}）"
            items.append({
                "channel": "distribution",
                "text": head,
                "strength": "weak",
                "caveat": (
                    "未过库外阈值，所以不能算命中；softmax 概率在闭集内必然归一到 1，"
                    "「最像」只说明候选里它最近，不说明真是它"
                    + (f"；且前两名差距仅 {robust['gap_ratio']:.1%}，"
                       f"分布上几乎并列，别把排名当成分辨"
                       if robust.get("gap_ratio") is not None
                       and robust["gap_ratio"] < 0.05 else "")
                ),
            })

        # 4) 接入链路。与模型身份无关，但「这个入口是谁搭的」本身就是一类答案。
        for ev in self.evidence:
            if ev.channel != "gateway":
                continue
            metric = ev.metric or {}
            if metric.get("verdict"):
                items.append({
                    "channel": "gateway",
                    "text": f"接入链路判定为「{metric.get('label') or metric['verdict']}」",
                    "strength": "weak",
                    "caveat": (
                        "链路结论不参与身份打分；中间层本身不是问题，改流量才是"
                    ),
                })

        if not items:
            return None

        # 3) 软共识：两条不同通道指向同一家族时值得点出来，但依然不是判定。
        families: dict[str, list[str]] = {}
        for item in items:
            for family, hints in _LEAD_FAMILY_HINTS.items():
                if any(hint in item["text"].lower() for hint in hints):
                    families.setdefault(family, []).append(item["channel"])
        consensus = [
            {"family": family, "from": sorted(set(channels))}
            for family, channels in families.items() if len(set(channels)) >= 2
        ]

        return {
            "items": items,
            "consensus": consensus,
            "note": (
                "以上是**未确认的线索**，不是判定：通道全部弃权时它们只说明"
                "「观测到了什么」，不构成「它是什么」。自述可伪造，超容差的"
                "词表匹配更是只能说明它不像已知词表。要得到判定，先把目标模型"
                "采进基准库。"
            ),
        }

    @property
    def lineage_note(self) -> dict[str, Any] | None:
        """「软信号像 X，但硬信号不像 X」时的一条解释性提示。

        动机是一个真实发生的误判（2026-10，详见 DESIGN.md 附录 G）：
        有人用 ModelTrace 跑 DeepSeek 的 API，参考库里 18 个候选全是
        GPT/Claude 家族、没有 DeepSeek，闭集归因把结果塞给了
        GPT-6 Astra（84.66%），于是出现「DeepSeek 蒸馏自 GPT-6 Astra」的结论。

        三个必须分开的问题，指纹只能回答中间那个：
          1. 厂商**宣布发布**的是什么产品；
          2. 回答某个请求的**可观测系统**是什么；
          3. 模型的**训练血缘**是什么。

        蒸馏会搬走第 3 类信息的一部分（答案分布、风格、行为口径），
        但搬不走词表、后端配置和推理栈时序。所以当可迁移通道说「像 X」
        而抗蒸馏通道连候选名单里都没有 X 时，这个组合更像
        **「从 X 蒸馏而来的独立模型」或「巧合」**，
        而不是「这个端点正在服务 X」。

        它不改变裁决，只解释裁决 —— 绝不能用它反推训练血缘。
        """
        soft_liked: dict[str, str] = {}   # 可迁移通道 → 它判定相符的首选候选
        endorsed: set[str] = set()        # 抗蒸馏通道明确认可的候选
        hard_spoke: list[str] = []        # 实际发言了的抗蒸馏通道

        for ev in self.evidence:
            if not ev.candidates:
                continue
            if ev.channel in DISTILLATION_TRANSFERABLE and ev.match is True:
                soft_liked[ev.channel] = ev.candidates[0].id
            elif ev.channel in DISTILLATION_RESISTANT:
                hard_spoke.append(ev.channel)
                # 只有「首选候选且判定相符」才算认可；有候选但 match=False
                # 或 None 都不算 —— 它没有为任何候选背书。
                if ev.match is True:
                    endorsed.add(ev.candidates[0].id)

        # 两类通道都得真的说过话，这条提示才有意义
        if not soft_liked or not hard_spoke:
            return None

        favored = sorted(set(soft_liked.values()))
        unsupported = [m for m in favored if m not in endorsed]
        if not unsupported:
            return None

        return {
            "candidates": unsupported,
            "favored_by": {ch: soft_liked[ch] for ch in sorted(soft_liked)},
            "resistant_channels": sorted(set(hard_spoke)),
            "note": (
                f"可迁移通道（{'、'.join(sorted(soft_liked))}）认为与 "
                f"{'、'.join(unsupported)} 相符，但抗蒸馏通道"
                f"（{'、'.join(sorted(set(hard_spoke)))}）并未认可"
                f"{'它' if len(unsupported) == 1 else '它们'}。"
                "答案分布与风格可以被蒸馏继承，词表与后端配置不能 —— "
                "这个组合指向「从该候选蒸馏而来的独立模型」或「巧合」，"
                "而非「本端点正在服务该候选」。"
                "统计指纹无法证明训练血缘，切勿据此下结论。"
            ),
        }

    @property
    def recommendations(self) -> list[dict[str, Any]]:
        """下一步该做什么。

        为什么单独一块：一份四层全弃权的报告，如果不能告诉使用者"现在该干嘛"，
        它就只是把「不知道」换了个说法。用户要的是可执行的动作，不是又一堆
        免责声明。每条建议都指向一个具体动作，并且说明做了之后能拿到什么。

        只给**现在**用得上的：缺什么、补什么、补完能解决哪一层。
        """
        out: list[dict[str, Any]] = []

        dist_ev = next((e for e in self.evidence if e.channel == "distribution"), None)
        tok_ev = next((e for e in self.evidence if e.channel == "tokenizer"), None)
        beh_ev = next((e for e in self.evidence if e.channel == "behavior"), None)

        # --- 数字分布：库外或没到阈值 ---
        if dist_ev is not None:
            metric = dist_ev.metric or {}
            robust = metric.get("robust") or {}
            best = robust.get("best_score")
            threshold = robust.get("out_of_scope_threshold")
            if best is not None and threshold is not None and best < threshold:
                out.append({
                    "title": "把目标模型的数字分布指纹采进库",
                    "why": (
                        f"本次最像 {robust.get('prediction') or '（见证据表）'}，"
                        f"得分 {best:.2f} 未过库外阈值 {threshold}。稳健打分包目前只覆盖 "
                        f"GPT / Claude 共 18 类，其余家族没有训练好的类心与标定，"
                        f"只能走库外守卫。"
                    ),
                    "how": (
                        "到「采集基准」页填同一个端点与模型 ID，勾选数字分布指纹，"
                        "采 36 条以上回答。采完这条模型就有自己的类心，下次直接可比。"
                    ),
                    "unlocks": "distribution 通道（权重最高的一条硬证据）",
                })
            elif metric.get("samples") and metric["samples"] < 3:
                out.append({
                    "title": "样本量不足，加大测试项目数量再跑一次",
                    "why": (
                        f"本次只拿到 {metric['samples']} 份可用回答，"
                        f"而概率标定至少需要 1–3 份。样本越少，噪声占比越大。"
                    ),
                    "how": "把「测试项数量」调到 5 以上重跑；接口不稳定就选自动测试的重试余量。",
                    "unlocks": "分布类通道的稳定性",
                })

        # --- 词表：超容差 ---
        if tok_ev is not None:
            best = (tok_ev.metric or {}).get("vocab_best") or {}
            diff = best.get("diff")
            if diff is not None and diff > 0.06:
                # 差得越远，「最接近谁」的信息量越低。超过一半就等于完全不像，
                # 这时候把「最接近 Qwen」写在前面只会误导。
                if diff > 0.5:
                    why = (
                        f"37 格探针与 12 份本地参考全部显著不符"
                        f"（最接近的「{best.get('label')}」也差 {diff:.1%}），"
                        f"等于**无法判断家族**。注意一个已知缺口：Anthropic 不公开词表，"
                        f"本地没有 Claude 的参考可建，所以测 Claude 系时这条通道"
                        f"结构性弃权，与端点好坏无关。"
                    )
                    how = (
                        "若目标是 Claude 系，这条通道帮不上忙，改看 behavior 与 distribution；"
                        "若是其它开源家族，用 scripts/build_tokenizer_references.py --fetch "
                        "补对应词表参考（需能访问 HuggingFace）。"
                    )
                else:
                    why = (
                        f"最接近「{best.get('label')}」（{best.get('family')} 家族），"
                        f"相对差 {diff:.1%}，超出 6% 容差，不构成匹配。"
                    )
                    how = "补对应词表参考，或把测试项目数量调大以降低噪声。"
                out.append({
                    "title": "词表不在本地 12 份参考内",
                    "why": why,
                    "how": how,
                    "unlocks": "tokenizer 通道（抗蒸馏最强的家族级证据）",
                })

        # --- 行为：采到了但库里没参考 ---
        if beh_ev is not None and beh_ev.note and "没有带 behavior 数据" in beh_ev.note:
            out.append({
                "title": "补一条行为特征参考",
                "why": (
                    "behavior 通道这次采到了完整向量（自述身份、上下文窗口、"
                    "代码风格等），但库里没有任何带 behavior 数据的条目可比 —— "
                    "等于白采。"
                ),
                "how": (
                    "在「采集基准」页对同一端点采一次，勾选行为特征。之后这条通道"
                    "就能做一致性比对，也是唯一能分辨同族近亲版本的软证据。"
                ),
                "unlocks": "behavior 通道（同族近亲版本分辨）",
            })

        # --- 完全没有可用证据时，兜底建议 ---
        if not out:
            out.append({
                "title": "先确认端点可达、再逐项收窄",
                "why": "本次没有任何通道产出可用观测。",
                "how": (
                    "检查 Base URL / API Key / 模型 ID 是否填写正确；"
                    "再用「手动测试」只勾词表指纹与后端指纹各跑一次，确认基础连通。"
                ),
                "unlocks": "定位是端点问题还是库覆盖问题",
            })
        return out

    @property
    def openset(self) -> dict[str, Any] | None:
        """开集识别块：候选池里到底有没有正确答案。

        与 `verdict` 的分工：verdict 在库内选一个（闭集，一定给答案），
        这里先问「该不该给答案」。两者的差别就是 2026-10 那次误判的全部
        （见 DESIGN.md 附录 G）。

        用的是**原始相似度**，不是 softmax 之后的概率 —— softmax 会把
        扁平分布人为压成尖锐分布，正好抹掉要观测的信号。
        """
        from .fusion import openset as osmod
        from .fusion.calibrate import load_calibration

        # 每个候选在各通道上的原始得分取平均（与 decide 的融合口径一致）
        per_candidate: dict[str, list[float]] = {}
        for ev in self.evidence:
            if ev.channel in ("gateway", "validity", "consistency"):
                continue
            for cand in ev.candidates:
                per_candidate.setdefault(cand.id, []).append(float(cand.score))
        if len(per_candidate) < 2:
            return None

        scores = {
            mid: sum(v) / len(v) for mid, v in per_candidate.items() if v
        }
        stats = osmod.vector_stats(list(scores.values()))

        fitted = (load_calibration().get("openset") or {})
        # 阈值是按某个库标定的，换库就失效 —— 只认同一个库的标定结果
        same_bank = (
            fitted.get("bank") is not None
            and fitted.get("bank") == (self.baseline or {}).get("bank")
        )
        is_fitted = bool(fitted.get("fitted") and same_bank)
        assessment = osmod.assess(
            stats,
            top_floor=fitted.get("top_floor") if is_fitted else None,
            gap_floor=fitted.get("gap_floor") if is_fitted else None,
        )

        # 经验预测集：库外时第二名通常落后 set_delta，落在这个窗口里的
        # 候选无法被排除。**不是**共形预测 —— 没有形式覆盖率保证，
        # 因为一条基准目前只存一份观测，构造不出交换性。
        delta = fitted.get("set_delta")
        if delta is None:
            delta = max(0.05, 0.10 * max(scores.values()))
        top_score = max(scores.values())
        members = sorted(
            ((mid, s) for mid, s in scores.items() if s >= top_score - delta),
            key=lambda kv: -kv[1],
        )

        # 数字分布通道带训练好的判别器，它的库外守卫是**经过标定**的，
        # 比形状判据更硬：命中就直接改写结论，并写进理由里。
        guard: dict[str, Any] | None = None
        for ev in self.evidence:
            robust_block = (ev.metric or {}).get("robust")
            if not isinstance(robust_block, dict):
                continue
            if robust_block.get("out_of_scope") is True:
                guard = robust_block
                break

        reasons = list(assessment["reasons"])
        verdict = assessment["verdict"]
        if guard is not None:
            top = guard.get("prediction") or "—"
            reasons.append(
                f"数字分布判别器判为库外（最高分 {guard.get('best_score')} < "
                f"阈值 {guard.get('out_of_scope_threshold')}），"
                f"闭集里最像的是「{top}」，但那个 "
                f"{round(float(guard.get('probability') or 0) * 100, 2)}% "
                "只表示「在这批候选里最像它」，不代表它是"
            )
            if verdict == "in_bank":
                verdict = "likely_unseen"

        return {
            "verdict": verdict,
            "reasons": reasons,
            "guard": guard,
            "stats": stats,
            "scores": {k: round(v, 4) for k, v in sorted(
                scores.items(), key=lambda kv: -kv[1]
            )},
            "prediction_set": [mid for mid, _ in members],
            "set_delta": round(delta, 4),
            "calibrated": is_fitted,
            "calibration": fitted or None,
            "note": (
                "判据来自 LLMmap（USENIX Sec '25）附录 E：固定阈值不可靠，"
                "要看候选得分向量的形状。两档都不满足才判库外。"
                "预测集是经验性的，不含共形预测的覆盖率保证。"
            ),
        }

    @property
    def exit_code(self) -> int:
        """0 成功 · 1 运行失败 · 2 弃权 · 3 真伪不符。"""
        if self.truthfulness and self.truthfulness.verdict in {
            "inconsistent",
            "suspicious",
        }:
            return 3
        if self.top_layer is None:
            return 2
        return 0

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input": self.input,
            "protocol": self.protocol,
            "layers": {
                layer: self.layers[layer].to_dict()
                for layer in LAYER_ORDER
                if layer in self.layers
            },
            "evidence": [e.to_dict() for e in self.evidence],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "cost": self.cost,
            "baseline": self.baseline,
            "disclaimer": DISCLAIMER,
        }
        if self.truthfulness:
            data["truthfulness"] = self.truthfulness.to_dict()
        if self.gateway:
            data["gateway"] = self.gateway
        data["verdict"] = self.verdict
        if self.validity:
            data["validity"] = self.validity
        if self.warnings:
            data["warnings"] = self.warnings
        note = self.lineage_note
        if note:
            data["lineage_note"] = note
        block = self.openset
        if block:
            data["openset"] = block
        mixed = self.multi_backend
        if mixed:
            data["multi_backend"] = mixed
        leads = self.leads
        if leads:
            data["leads"] = leads
        advice = self.recommendations
        if advice:
            data["recommendations"] = advice
        return data

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"# ModelProof 验证报告 `{self.run_id}`")
        lines.append("")
        lines.append(f"- 开始：{self.started_at}")
        if self.finished_at:
            lines.append(f"- 结束：{self.finished_at}")
        for key in ("base_url", "declared_model", "claim", "suite", "target_layer"):
            if key in self.input and self.input[key]:
                lines.append(f"- {key}：`{self.input[key]}`")
        lines.append("")

        verdict = self.verdict
        lines.append("## 裁决")
        lines.append("")
        lines.append(f"**{verdict['state']}** —— {verdict['basis']}")
        lines.append("")
        for reason in verdict.get("reasons", []):
            lines.append(f"- {reason}")
        lines.append("")

        mixed = self.multi_backend
        if mixed:
            lines.append("## 多后端分派")
            lines.append("")
            lines.append(
                f"**这不是「测不出来」，是这个问题本身没有单一答案。** "
                f"{mixed['considered']} 格词表探针里，"
                f"{mixed['considered'] - mixed['unresolved']} 格都能被某个已知词表"
                f"解释（偏差 ≤{mixed['tolerance']}），但解释它们的不是同一个词表："
            )
            lines.append("")
            lines.append("| 家族 | 命中的格子数 |")
            lines.append("|---|---|")
            for family, count in mixed["votes"].items():
                lines.append(f"| {family} | {count} |")
            lines.append("")
            lines.append(
                f"整体最接近的单一词表是 `{mixed.get('best_reference')}`，"
                f"相对差 {mixed['best_diff']:.1%}，远超 6% 容差。"
                "结论：请求被按某种规则分派到了不同后端，"
                "**不存在一个可以被鉴定的身份**。这与「请求一致性」通道报出的"
                "同题不同值互为印证。"
            )
            lines.append("")

        leads = self.leads
        if leads:
            lines.append("## 未确认线索")
            lines.append("")
            lines.append(f"> {leads['note']}")
            lines.append("")
            for item in leads["items"]:
                lines.append(
                    f"- [{CHANNEL_LABEL.get(item['channel'], item['channel'])}] "
                    f"{item['text']} —— {item['caveat']}"
                )
            for consensus in leads.get("consensus") or []:
                lines.append(
                    f"- 软共识：`{consensus['family']}` 家族"
                    f"（来自 {'、'.join(CHANNEL_LABEL.get(c, c) for c in consensus['from'])}）"
                    " —— 两条通道同向，但仍不足以判定"
                )
            lines.append("")

        advice = self.recommendations
        if advice:
            lines.append("## 下一步建议")
            lines.append("")
            for index, item in enumerate(advice, 1):
                lines.append(f"### {index}. {item['title']}")
                lines.append("")
                lines.append(f"- 为什么：{item['why']}")
                lines.append(f"- 怎么做：{item['how']}")
                lines.append(f"- 能解锁：{item['unlocks']}")
                lines.append("")

        lineage = self.lineage_note
        if lineage:
            lines.append("## 血缘提示")
            lines.append("")
            lines.append(f"> {lineage['note']}")
            lines.append("")
            lines.append(
                f"- 可迁移通道偏好："
                + "，".join(
                    f"{CHANNEL_LABEL.get(ch, ch)} → {mid}"
                    for ch, mid in lineage["favored_by"].items()
                )
            )
            lines.append(
                f"- 已发言的抗蒸馏通道："
                + "、".join(
                    CHANNEL_LABEL.get(ch, ch)
                    for ch in lineage["resistant_channels"]
                )
            )
            lines.append("")

        block = self.openset
        if block:
            lines.append("## 开集识别")
            lines.append("")
            state = {
                "in_bank": "库内有对应条目",
                "likely_unseen": "候选池里大概没有正确答案",
                "ambiguous": "说不准",
                "unknown": "无法判断",
            }.get(block["verdict"], block["verdict"])
            lines.append(f"**{state}**")
            lines.append("")
            for reason in block["reasons"]:
                lines.append(f"- {reason}")
            lines.append("")
            stats = block["stats"]
            lines.append(
                f"候选得分向量：n={int(stats.get('n', 0))} · "
                f"最佳 {stats.get('max')} · 均值 {stats.get('mean')} · "
                f"标准差 {stats.get('std')} · 峰度 z={stats.get('z')} · "
                f"归一化熵 {stats.get('entropy')}"
            )
            lines.append("")
            lines.append(
                "经验预测集（无法排除的候选）："
                + ("、".join(f"`{m}`" for m in block["prediction_set"]) or "（无）")
                + f"；半宽 {block['set_delta']}"
            )
            lines.append("")
            guard = block.get("guard")
            if guard:
                lines.append(
                    f"**数字分布判别器判为库外**：最高分 {guard.get('best_score')} "
                    f"< 阈值 {guard.get('out_of_scope_threshold')}，"
                    f"闭集里最像的是 `{guard.get('prediction')}`"
                    f"（{guard.get('probability'):.1%}）—— 这个百分比只表示"
                    "「在这批候选里最像它」，不代表它是。"
                )
                lines.append("")
            lines.append(f"> {block['note']}")
            lines.append("")

        lines.append("## 结论")
        lines.append("")
        lines.append("| 层 | 判定 | 置信度 | 状态 |")
        lines.append("|---|---|---|---|")
        for layer in LAYER_ORDER:
            verdict = self.layers.get(layer)
            if not verdict:
                continue
            if verdict.abstained:
                status = f"弃权（{verdict.abstain_reason or '—'}）"
                value = "—"
            else:
                status = "判定"
                value = verdict.value or "—"
            lines.append(f"| {layer} | {value} | {verdict.confidence:.0%} | {status} |")
        lines.append("")

        if self.truthfulness and self.truthfulness.verdict != "unknown":
            truth = self.truthfulness
            lines.append("## 真伪判定")
            lines.append("")
            lines.append(f"- 声明：`{truth.claim}`")
            lines.append(f"- 结论：**{truth.verdict}**（{truth.confidence:.0%}）")
            if truth.checks:
                lines.append("")
                lines.append("| 字段 | 声明 | 观测 | 一致 |")
                lines.append("|---|---|---|---|")
                for check in truth.checks:
                    flag = {True: "✅", False: "❌", None: "—"}.get(check.match, "—")
                    lines.append(
                        f"| {check.field} | {check.claimed} | {check.observed} | {flag} |"
                    )
            if truth.note:
                lines.append("")
                lines.append(f"> {truth.note}")
            lines.append("")

        lines.append("## 证据")
        lines.append("")
        if not self.evidence:
            lines.append("（无）")
        else:
            lines.append("| 通道 | 测试项 | 层 | 观测 | 命中 | 权重 |")
            lines.append("|---|---|---|---|---|---|")
            for item in self.evidence:
                flag = {True: "✅", False: "❌", None: "—"}.get(item.match, "—")
                observed = item.observed.replace("|", "\\|").replace("\n", " ")
                if len(observed) > 60:
                    observed = observed[:57] + "..."
                lines.append(
                    f"| {CHANNEL_LABEL.get(item.channel, item.channel)} "
                    f"| {item.item} | {item.target_layer or '—'} "
                    f"| {observed} | {flag} | {item.weight:.2f} |"
                )
        lines.append("")

        if self.conflicts:
            lines.append("## 冲突")
            lines.append("")
            for conflict in self.conflicts:
                channels = "、".join(
                    CHANNEL_LABEL.get(c, c) for c in conflict.channels
                ) or "—"
                lines.append(
                    f"- **{conflict.layer}** 层：{' vs '.join(conflict.values)}"
                    f"（{channels}，{conflict.severity}，"
                    f"置信度 ×{conflict.penalty}）"
                )
                if conflict.hint:
                    lines.append(f"  - {conflict.hint}")
            lines.append("")

        if self.validity:
            block = self.validity
            lines.append("## 结果可信度")
            lines.append("")
            lines.append(f"- 等级：**{block.get('level')}**")
            for factor in block.get("factors", []):
                lines.append(f"  - {factor}")
            checks = block.get("checks") or {}
            if checks.get("independent_channels") is not None:
                lines.append(
                    f"- 独立信号源 {checks['independent_channels']} 个"
                    f"（{', '.join(checks.get('channels') or []) or '—'}）"
                )
            if checks.get("margin") is not None:
                lines.append(f"- 顶层余量 {checks['margin']:.2%}")
            if block.get("note"):
                lines.append("")
                lines.append(f"> {block['note']}")
            lines.append("")

        if self.cost:
            cost = self.cost
            lines.append("## 消耗")
            lines.append("")
            lines.append(
                f"- 请求 {cost.get('requests', 0)} 次 · "
                f"输入 {cost.get('input_tokens', 0)} tok · "
                f"输出 {cost.get('output_tokens', 0)} tok"
            )
            lines.append(
                f"- 耗时 {cost.get('elapsed_seconds', 0):.1f}s · "
                f"费用 {cost.get('amount', 0):.4f} {cost.get('currency', 'USD')}"
            )
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(f"_{DISCLAIMER}_")
        return "\n".join(lines)

    # -- 持久化 -----------------------------------------------------------

    def save(self, directory: str | Path) -> Path:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{self.run_id}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "VerificationReport":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VerificationReport":
        from .types import Candidate

        report = cls(
            schema_version=raw.get("schema_version", SCHEMA_VERSION),
            run_id=raw.get("run_id", _new_run_id()),
            started_at=raw.get("started_at", _now()),
            finished_at=raw.get("finished_at"),
            input=raw.get("input", {}),
            protocol=raw.get("protocol", {}),
            truthfulness=None,
            cost=raw.get("cost", {}),
            baseline=raw.get("baseline", {}),
            gateway=raw.get("gateway"),
            validity=raw.get("validity"),
            warnings=raw.get("warnings", []),
        )

        for layer, payload in (raw.get("layers") or {}).items():
            report.layers[layer] = LayerVerdict(
                value=payload.get("value"),
                confidence=payload.get("confidence", 0.0),
                abstained=payload.get("abstained", False),
                abstain_reason=payload.get("abstain_reason"),
                detail=payload.get("detail", ""),
                threshold=payload.get("threshold", 0.0),
                top_candidates=[
                    Candidate(c["id"], c["score"])
                    for c in payload.get("top_candidates", [])
                ],
            )

        truth = raw.get("truthfulness")
        if truth:
            from .types import TruthfulnessCheck

            report.truthfulness = Truthfulness(
                claim=truth.get("claim"),
                verdict=truth.get("verdict", "unknown"),
                confidence=truth.get("confidence", 0.0),
                note=truth.get("note", ""),
                checks=[
                    TruthfulnessCheck(
                        field=c.get("field", ""),
                        claimed=c.get("claimed"),
                        observed=c.get("observed"),
                        match=c.get("match"),
                    )
                    for c in truth.get("checks", [])
                ],
            )

        report.evidence = [
            Evidence(
                channel=e.get("channel", ""),
                item=e.get("item", ""),
                target_layer=e.get("layer"),
                observed=e.get("observed", ""),
                metric=e.get("metric", {}),
                candidates=[
                    Candidate(c["id"], c["score"]) for c in e.get("candidates", [])
                ],
                match=e.get("match"),
                weight=e.get("weight", 0.0),
                note=e.get("note", ""),
                axis=e.get("axis"),
                level=e.get("level"),
                baseline_id=e.get("baseline_id"),
            )
            for e in raw.get("evidence", [])
        ]

        report.conflicts = [
            Conflict(
                layer=c.get("layer", "family"),
                channels=c.get("channels", []),
                values=c.get("values", []),
                severity=c.get("severity", "soft"),
                penalty=c.get("penalty", 1.0),
                hint=c.get("hint", ""),
            )
            for c in raw.get("conflicts", [])
        ]
        return report


def attach_estimate(report: VerificationReport, estimate: CostRange) -> None:
    """把开跑前的预估写进报告，便于事后核对预估准不准。"""
    report.cost.setdefault(
        "estimate_at_start",
        {
            "amount_range": list(estimate.amount),
            "seconds_range": list(estimate.seconds),
            "requests": estimate.requests,
        },
    )


def attach_actual_cost(report: VerificationReport, actual: Cost) -> None:
    report.cost.update(
        {
            "requests": actual.requests,
            "input_tokens": actual.input_tokens,
            "output_tokens": actual.output_tokens,
            "elapsed_seconds": round(actual.seconds, 2),
            "currency": actual.currency,
            "amount": round(actual.amount, 6),
        }
    )
