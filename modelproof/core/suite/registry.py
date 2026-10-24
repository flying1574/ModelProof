"""测试项注册表与内置套件。

注册表同时是「可自由组合」的实现基础：CLI 的 `--items`、Web 的勾选框、
API 的 `items[]` 都指向同一批 ID。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..channel_meta import CHANNEL_LABEL, CHANNEL_ORDER
from ..estimate import _load_yaml
from .model import Suite, TestItem

ITEMS_DIR = Path(__file__).resolve().parents[3] / "suites" / "items"
SUITES_FILE = Path(__file__).resolve().parents[3] / "suites" / "suites.yaml"

DEFAULT_SUITE = "standard"

# 界面只让用户填「要几项」，所以**取哪几项**必须有确定顺序。
# 硬信号与零成本通道在前，软信号在后 —— 数量被压到很小时不会只剩软信号。
ITEM_PRIORITY: list[str] = [
    "tokenizer.marginal_cost",   # 硬信号，抗蒸馏
    "backend_fp.read",           # 搭车采集，零额外成本
    "distribution.random_int",   # 唯一带标定概率的通道
    "behavior.interrogate",
    "gateway.probe",
    "onetoken.fingerprint",
    "consistency.requests",
    "capability.cliff",
    "validity.suite",
    "trace.stream_timing",
    "style.density",
]


# --------------------------------------------------------------------------
# 内置测试项（v0.1）
# --------------------------------------------------------------------------

BUILTIN_ITEMS: list[TestItem] = [
    TestItem(
        id="tokenizer.marginal_cost",
        channel="tokenizer",
        name="协议层词表指纹",
        yields=["vendor", "family"],
        requires=["usage"],
        requests=37,
        input_tokens_range=(1800, 2600),
        output_tokens_range=(37, 185),
        manual_friendly=False,
        description=(
            "用 37 条多文种探针测边际 token 成本。词表训练时即固定，"
            "改词表等于重训，所以系统提示词 / 量化 / 套壳都改不了它。"
            "同家族不同版本共用词表，因此只能证明到 family 层。"
        ),
    ),
    TestItem(
        id="backend_fp.read",
        channel="backend_fp",
        name="后端指纹读取",
        yields=["version"],
        requires=["body"],
        requests=0,
        input_tokens_range=(0, 0),
        output_tokens_range=(0, 0),
        manual_friendly=True,
        description=(
            "读取 system_fingerprint（OpenAI 定义为「模型运行的后端配置」）。"
            "搭车采集，零额外成本。主要价值是变更检测：指纹一变说明上游换部署了。"
        ),
    ),
    TestItem(
        id="gateway.probe",
        channel="gateway",
        name="接入链路探测",
        yields=[],
        requires=["http"],
        requests=8,
        input_tokens_range=(120, 400),
        output_tokens_range=(0, 300),
        manual_friendly=False,
        description=(
            "判定「这个入口是谁搭的」，与模型身份正交。"
            "链路轴（中间有几层）与行为轴（在不在改你的流量）分开报。"
        ),
    ),
    TestItem(
        id="behavior.interrogate",
        channel="behavior",
        name="行为与自述特征",
        yields=["series", "version"],
        requires=["chat"],
        requests=12,
        input_tokens_range=(600, 900),
        output_tokens_range=(400, 1200),
        manual_friendly=True,
        description=(
            "12 条无害探针：自述身份、知识截止、上下文窗口、字母计数、"
            "浮点严谨性、代码风格、对齐话术、格式习惯等。"
            "自述可伪造，属软信号，只能降低置信度不能单独支撑结论。"
        ),
    ),
    TestItem(
        id="distribution.random_int",
        channel="distribution",
        name="数字分布指纹",
        yields=["family", "series", "version"],
        requires=["chat"],
        requests=3,
        input_tokens_range=(150, 300),
        output_tokens_range=(2400, 3000),
        manual_friendly=False,
        description=(
            "3 条长整数生成挑战，Hellinger 距离 + 有序块特征。"
            "这是词表相同时仍能分开近亲版本的主要手段。"
        ),
    ),
    TestItem(
        id="onetoken.fingerprint",
        channel="onetoken",
        name="单 token 分布指纹",
        yields=["family", "series"],
        requires=["chat"],
        requests=120,
        input_tokens_range=(2880, 4800),
        output_tokens_range=(120, 240),
        manual_friendly=False,
        description=(
            "8 个 cell（随机数/颜色/字母/硬币 × 中英）× 15 次单 token 采样，"
            "比较答案的经验分布（base-2 JSD）。依据 arXiv 2607.10252："
            "同模型 ≈0.14 / 换供应商 ≈0.227 / 不同模型 ≈0.463，40 cells 的 EER 7.3%、"
            "8 cells 仍 <11%。每次只出 1 个 token，实际费用低于一次长生成。"
            "附带 split-half 自洽性检查，可发现端点在多个后端之间轮换。"
        ),
    ),
    TestItem(
        id="consistency.requests",
        channel="consistency",
        name="请求一致性",
        yields=[],
        requires=["chat"],
        requests=15,
        input_tokens_range=(600, 1350),
        output_tokens_range=(600, 900),
        manual_friendly=True,
        description=(
            "不看入口静态长什么样，看它对你发的每个请求是否始终如一："
            "max_tokens 有没有被缩减、temperature 有没有被锁死、"
            "简单题是否反而比复杂题答得差（难度倒挂 = 疑似动态路由）、"
            "同一事实重复问是否翻转。四条都只报「疑似」，不指认作弊。"
        ),
    ),
    TestItem(
        id="capability.cliff",
        channel="capability",
        name="能力悬崖",
        yields=["series", "version"],
        requires=["chat"],
        requests=6,
        input_tokens_range=(800, 2000),
        output_tokens_range=(600, 1500),
        manual_friendly=True,
        description=(
            "汉诺塔 5 盘、字母计数、长上下文召回、多跳算术、严格 JSON。"
            "用于分离系列（Opus vs Haiku）。"
        ),
    ),
    TestItem(
        id="trace.stream_timing",
        channel="trace",
        name="调用链时序 trace",
        yields=[],
        requires=["stream"],
        requests=3,
        input_tokens_range=(60, 150),
        output_tokens_range=(120, 360),
        manual_friendly=False,
        description=(
            "流式采集 TTFT、chunk 间隔、稳定期 TPS。推理后端是一个「沉默的"
            "超参数」，同一模型换后端分数可差 16pp，因此 trace 用于后端一致性"
            "判断与性能画像 —— 它不做身份判定。"
        ),
    ),
    TestItem(
        id="validity.suite",
        channel="validity",
        name="结果验真套件",
        yields=[],
        requires=["chat"],
        requests=8,
        input_tokens_range=(200, 400),
        output_tokens_range=(150, 500),
        manual_friendly=True,
        description=(
            "回答「这次测出来的东西能不能信」：释义鲁棒性（污染/记忆化）、"
            "重复采样与翻转率（可复现性）、不可回答问题的幻觉检测、"
            "canary 编造检测（数据污染）。探针全部可自动判定，不用 LLM 当裁判。"
        ),
    ),
    TestItem(
        id="style.density",
        channel="style",
        name="排版风格密度",
        yields=["family", "series"],
        requires=["chat"],
        requests=1,
        input_tokens_range=(80, 150),
        output_tokens_range=(400, 900),
        manual_friendly=True,
        description=(
            "21 个排版特征。只在「同一条 prompt 的两个回答」之间有意义，"
            "强依赖 prompt，权重最低。"
        ),
    ),
]


# --------------------------------------------------------------------------
# 内置套件
# --------------------------------------------------------------------------

BUILTIN_SUITES: list[Suite] = [
    Suite(
        id="quick",
        name="快速",
        items=["tokenizer.marginal_cost", "backend_fp.read"],
        description="37 次轻量请求，回答「这是什么家族」。日常巡检首选。",
    ),
    Suite(
        id="standard",
        name="标准",
        items=[
            "tokenizer.marginal_cost",
            "backend_fp.read",
            "behavior.interrogate",
            "gateway.probe",
            "distribution.random_int",
        ],
        description=(
            "家族 + 系列 + 链路 + 数字分布，一次跑完。性价比最高的默认选择。"
            "数字分布是唯一带标定概率与库外守卫的通道，默认带上它，"
            "标准套件才能真正回答「它是什么」而不只是「它像什么」。"
        ),
    ),
    Suite(
        id="onetoken",
        name="单 token 指纹",
        items=["onetoken.fingerprint", "backend_fp.read"],
        description=(
            "只跑 C11：120 次单 token 采样（每次只出 1 个 token，费用极低）。"
            "论文给的 EER 是 7.3%（40 cells）/ <11%（8 cells）。"
            "想用最低成本判断「是不是同一个模型」时选它。"
        ),
    ),
    Suite(
        id="audit",
        name="中转审计",
        items=[
            "onetoken.fingerprint",
            "consistency.requests",
            "gateway.probe",
            "backend_fp.read",
        ],
        description=(
            "专为「怀疑中转商动手脚」设计：单 token 指纹比对 + 请求一致性"
            "（参数是否被改、是否动态路由）+ 静态链路取证。"
            "回答的是「它对你的每个请求是否始终如一」。"
        ),
    ),
    Suite(
        id="deep",
        name="深度",
        items=[
            "tokenizer.marginal_cost",
            "backend_fp.read",
            "behavior.interrogate",
            "gateway.probe",
            "onetoken.fingerprint",
            "distribution.random_int",
            "capability.cliff",
            "style.density",
        ],
        description="全通道，尝试定到版本层。输出 token 多，费用明显上升。",
    ),
    Suite(
        id="truth",
        name="真伪核验",
        items=[
            "tokenizer.marginal_cost",
            "behavior.interrogate",
            "backend_fp.read",
        ],
        description="配合 --claim 使用：验证它是不是自己声称的那个。",
    ),
    Suite(
        id="validity",
        name="结果验真",
        items=["validity.suite", "trace.stream_timing", "backend_fp.read"],
        description=(
            "不问身份，问「这次的结果能不能信」：释义鲁棒性、可复现性、"
            "幻觉检测，外加后端一致性 trace。适合评测前先自检端点。"
        ),
    ),
    Suite(
        id="full",
        name="全通道",
        items=[
            "tokenizer.marginal_cost",
            "backend_fp.read",
            "behavior.interrogate",
            "gateway.probe",
            "onetoken.fingerprint",
            "consistency.requests",
            "distribution.random_int",
            "capability.cliff",
            "style.density",
            "trace.stream_timing",
            "validity.suite",
        ],
        description="身份判定 + 链路 + 一致性 + 验真全开。成本最高，用于发布前体检。",
    ),
    Suite(
        id="link-only",
        name="仅链路",
        items=["gateway.probe"],
        description="不问身份，只问「这个入口是谁搭的」。成本极低。",
    ),
]


class Registry:
    """测试项与套件的唯一入口。外部 YAML 可覆盖或追加内置定义。"""

    def __init__(self) -> None:
        self._items: dict[str, TestItem] = {i.id: i for i in BUILTIN_ITEMS}
        self._suites: dict[str, Suite] = {s.id: s for s in BUILTIN_SUITES}
        self.errors: list[str] = []   # 外部配置的问题，报出来但不打断启动
        self.load_external()

    # -- 加载 -------------------------------------------------------------

    def load_external(self) -> None:
        if ITEMS_DIR.is_dir():
            for path in sorted(ITEMS_DIR.glob("*.yaml")):
                data = _load_yaml(path)
                if data and data.get("id"):
                    self._items[data["id"]] = _item_from_dict(data)
        if SUITES_FILE.exists():
            data = _load_yaml(SUITES_FILE)
            entries = (data or {}).get("suites", [])
            # 手写的 YAML 结构写错是常事。这里宁可跳过整个外部文件也不能让
            # 工具起不来 —— 内置套件还在，功能不受影响，只是少了自定义的那几个。
            if not isinstance(entries, list):
                self.errors.append(
                    f"{SUITES_FILE.name}：suites 应为一个列表，实际是 "
                    f"{type(entries).__name__}，已跳过外部套件"
                )
                return
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                items = entry.get("items") or []
                if not isinstance(items, list):
                    self.errors.append(
                        f"{SUITES_FILE.name}：套件 {entry['id']} 的 items 不是列表，已跳过"
                    )
                    continue
                self._suites[entry["id"]] = Suite(
                    id=entry["id"],
                    name=entry.get("name", entry["id"]),
                    items=[str(i) for i in items],
                    description=entry.get("description", ""),
                    builtin=False,
                )

    # -- 查询 -------------------------------------------------------------

    def items(self) -> list[TestItem]:
        return list(self._items.values())

    def item(self, item_id: str) -> TestItem | None:
        return self._items.get(item_id)

    def items_for_channels(self, channels: list[str]) -> list[TestItem]:
        return [i for i in self._items.values() if i.channel in channels]

    def resolve(self, spec: str | None) -> list[TestItem]:
        """把 `--items` 或套件名解析成测试项列表。

        支持套件名、测试项 ID、通道名三种写法，用逗号分隔混合使用。
        """
        if not spec:
            return self.suite(DEFAULT_SUITE).resolved_items(self)
        picked: list[TestItem] = []
        seen: set[str] = set()
        for token in [t.strip() for t in spec.split(",") if t.strip()]:
            suite = self._suites.get(token)
            if suite:
                candidates = suite.resolved_items(self)
            elif token in self._items:
                candidates = [self._items[token]]
            else:
                candidates = [
                    i for i in self._items.values() if i.channel == token
                ]
                if not candidates:
                    continue
            for item in candidates:
                if item.id not in seen:
                    seen.add(item.id)
                    picked.append(item)
        return picked

    def suites(self) -> list[Suite]:
        return list(self._suites.values())

    def suite(self, suite_id: str) -> Suite:
        if suite_id not in self._suites:
            raise KeyError(
                f"未知道的测试集：{suite_id}，可选：{', '.join(sorted(self._suites))}"
            )
        return self._suites[suite_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() for i in self.items()],
            "suites": [s.to_dict() for s in self.suites()],
        }

    # -- 按数量挑选 -------------------------------------------------------
    #
    # 界面只让用户填「要几项」，不再逐项勾选，所以**取哪几项**必须由工具
    # 决定。顺序就是契约：先给硬信号与零成本通道，再给软信号；套件被选中
    # 时，套件内部顺序优先，套件之外按优先级补齐。

    def order(self, suite_id: str | None = None) -> list[str]:
        ordered: list[str] = []
        if suite_id:
            suite = self._suites.get(suite_id)
            if suite:
                for item in suite.resolved_items(self):
                    if item.id not in ordered:
                        ordered.append(item.id)
        for item_id in ITEM_PRIORITY:
            if item_id in self._items and item_id not in ordered:
                ordered.append(item_id)
        # 外部自定义项不在优先级表里，追加在后面，不能丢
        for item_id in self._items:
            if item_id not in ordered:
                ordered.append(item_id)
        return ordered

    def pick(self, count: int | None = None, suite_id: str | None = None) -> list[TestItem]:
        """取前 count 个测试项。count 为空时取该套件（默认 standard）的全部。"""
        ordered = self.order(suite_id)
        if count is None:
            count = len(ordered) if suite_id else len(
                self.suite(DEFAULT_SUITE).resolved_items(self)
            )
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, len(ordered)))
        return [self._items[i] for i in ordered[:count] if i in self._items]

    def categories(self, items: list[TestItem] | None = None) -> list[dict[str, Any]]:
        """把测试项压成「测试大类」——界面只显示这一层。"""
        picked = items if items is not None else self.items()
        buckets: dict[str, dict[str, Any]] = {}
        for item in picked:
            bucket = buckets.setdefault(
                item.channel,
                {"id": item.channel, "name": CHANNEL_LABEL.get(item.channel, item.channel),
                 "items": 0, "requests": 0},
            )
            bucket["items"] += 1
            bucket["requests"] += int(item.requests or 0)
        ordered = [
            buckets[ch] for ch in CHANNEL_ORDER if ch in buckets
        ] + [b for ch, b in buckets.items() if ch not in CHANNEL_ORDER]
        return ordered


# --------------------------------------------------------------------------
# 采集大类（通道）
# --------------------------------------------------------------------------

COLLECT_CHANNELS: dict[str, int] = {
    "tokenizer": 37,
    "behavior": 12,
    "backend_fp": 0,     # 搭车采集，零额外请求
    "distribution": 3,
    "onetoken": 120,
    "capability": 6,
    "style": 1,
    "trace": 3,
}

COLLECT_PRIORITY: list[str] = [
    "tokenizer", "behavior", "backend_fp", "distribution",
    "onetoken", "capability", "style", "trace",
]

DEFAULT_COLLECT_COUNT = 3


def pick_channels(count: int | None = None) -> list[str]:
    """取前 count 个采集大类。默认 3 个（词表 / 行为 / 后端指纹）。"""
    if count is None:
        count = DEFAULT_COLLECT_COUNT
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = DEFAULT_COLLECT_COUNT
    count = max(1, min(count, len(COLLECT_PRIORITY)))
    return COLLECT_PRIORITY[:count]


def collect_categories(channels: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "id": ch,
            "name": CHANNEL_LABEL.get(ch, ch),
            "items": 1,
            "requests": COLLECT_CHANNELS.get(ch, 0),
        }
        for ch in channels
    ]


def _item_from_dict(data: dict[str, Any]) -> TestItem:
    cost = data.get("cost") or {}
    in_range = cost.get("input_tokens_range") or [
        cost.get("input_tokens", 0),
        cost.get("input_tokens", 0),
    ]
    out_range = cost.get("output_tokens_range") or [
        cost.get("output_tokens", 0),
        cost.get("output_tokens", 0),
    ]
    return TestItem(
        id=data["id"],
        channel=data.get("channel", ""),
        name=data.get("name", data["id"]),
        yields=data.get("yields", []),
        requires=data.get("requires", []),
        requests=int(cost.get("requests", 0)),
        input_tokens_range=(int(in_range[0]), int(in_range[1])),
        output_tokens_range=(int(out_range[0]), int(out_range[1])),
        manual_friendly=bool(data.get("manual_friendly", False)),
        prompt=data.get("prompt", ""),
        parser=data.get("parser", ""),
        description=data.get("description", ""),
        enabled_by_default=bool(data.get("enabled_by_default", True)),
    )


REGISTRY = Registry()
