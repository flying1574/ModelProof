"""消耗预估。

开跑之前先告诉用户「这次大概要花多少、多久」，并且允许 `--max-cost`
`--max-seconds` 中途体面地停。预估是区间不是单点：输出 token 长度本身就
不可控，给单点等于骗人。

成本模型：
    amount  = in_tok / 1e6 * price_in + out_tok / 1e6 * price_out
    seconds = requests * per_request + out_tok / tps
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import Cost, CostRange

PRICING_DIR = Path(__file__).resolve().parents[2] / "pricing"

# 没有价格表时的兜底：按主流商用 API 的中位价给个保守值。
FALLBACK_PRICE = {
    "input_per_mtok": 3.0,
    "output_per_mtok": 15.0,
    "currency": "USD",
    "avg_seconds_per_request": 2.5,
    "tokens_per_second": 45.0,
}

# 推理模型会把输出 token 放大（思考过程也计费），预估时乘这个系数。
REASONING_MULTIPLIER = 3.5


@dataclass
class PriceTable:
    name: str = "fallback"
    input_per_mtok: float = FALLBACK_PRICE["input_per_mtok"]
    output_per_mtok: float = FALLBACK_PRICE["output_per_mtok"]
    currency: str = "USD"
    avg_seconds_per_request: float = FALLBACK_PRICE["avg_seconds_per_request"]
    tokens_per_second: float = FALLBACK_PRICE["tokens_per_second"]
    models: dict[str, dict[str, Any]] = None  # type: ignore[assignment]

    def for_model(self, model: str | None) -> "PriceTable":
        """按模型名找更精确的价格；找不到就用表级默认值。"""
        if not model or not self.models:
            return self
        hit = self.models.get(model)
        if not hit:
            # 允许前缀匹配，比如 gpt-6-astra-2026-05 命中 gpt-6-astra
            for key, value in self.models.items():
                if model.startswith(key) or key in model:
                    hit = value
                    break
        if not hit:
            return self
        return PriceTable(
            name=f"{self.name}:{model}",
            input_per_mtok=hit.get("input_per_mtok", self.input_per_mtok),
            output_per_mtok=hit.get("output_per_mtok", self.output_per_mtok),
            currency=hit.get("currency", self.currency),
            avg_seconds_per_request=hit.get(
                "avg_seconds_per_request", self.avg_seconds_per_request
            ),
            tokens_per_second=hit.get("tokens_per_second", self.tokens_per_second),
            models={},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "currency": self.currency,
            "avg_seconds_per_request": self.avg_seconds_per_request,
            "tokens_per_second": self.tokens_per_second,
        }


def load_price_table(name: str) -> PriceTable:
    """从 pricing/ 目录读价格表。读不到就退回兜底值并记一笔。"""
    path = PRICING_DIR / f"{name}.yaml"
    if not path.exists():
        return PriceTable(name=f"fallback({name} 未找到)")
    data = _load_yaml(path)
    if not data:
        return PriceTable(name=f"fallback({name} 解析失败)")
    return PriceTable(
        name=data.get("name", name),
        input_per_mtok=float(data.get("input_per_mtok", FALLBACK_PRICE["input_per_mtok"])),
        output_per_mtok=float(
            data.get("output_per_mtok", FALLBACK_PRICE["output_per_mtok"])
        ),
        currency=data.get("currency", "USD"),
        avg_seconds_per_request=float(
            data.get("avg_seconds_per_request", FALLBACK_PRICE["avg_seconds_per_request"])
        ),
        tokens_per_second=float(
            data.get("tokens_per_second", FALLBACK_PRICE["tokens_per_second"])
        ),
        models=data.get("models") or {},
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _tiny_yaml(path)
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _read_nodes(path: Path) -> list[tuple[int, str]]:
    """把文件读成 (缩进, 去注释后的文本) 列表。"""
    nodes: list[tuple[int, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split(" #")[0].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        nodes.append((indent, stripped))
    return nodes


def _parse_block(nodes: list[tuple[int, str]], pos: int, indent: int) -> tuple[Any, int]:
    """解析一个块。以 `- ` 开头则是序列，否则是映射。"""
    if pos >= len(nodes):
        return {}, pos
    if nodes[pos][1].startswith("- "):
        return _parse_seq(nodes, pos, indent)
    return _parse_map(nodes, pos, indent)


def _parse_child(
    nodes: list[tuple[int, str]], pos: int, parent_indent: int
) -> tuple[Any, int]:
    """`key:` 后面挂的子块。

    两种形态都要支持：缩进更深的映射块，以及与父级同缩进的 `- ` 序列
    （YAML 两种写法都合法，我们的 suites.yaml 用的是后者）。
    """
    if pos >= len(nodes):
        return {}, pos
    cur_indent, text = nodes[pos]
    if text.startswith("- "):
        return _parse_seq(nodes, pos, cur_indent)
    if cur_indent > parent_indent:
        return _parse_map(nodes, pos, cur_indent)
    return {}, pos


def _parse_map(
    nodes: list[tuple[int, str]], pos: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while pos < len(nodes):
        cur_indent, text = nodes[pos]
        # 缩进退回上一层，或者遇到序列项，都说明这个映射结束了
        if cur_indent < indent or text.startswith("- "):
            break
        if ":" not in text:
            pos += 1
            continue
        key, _, rest = text.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest == "":
            result[key], pos = _parse_child(nodes, pos + 1, cur_indent)
        else:
            result[key] = _parse_scalar(rest)
            pos += 1
    return result, pos


def _parse_seq(
    nodes: list[tuple[int, str]], pos: int, indent: int
) -> tuple[list[Any], int]:
    items: list[Any] = []
    while pos < len(nodes):
        cur_indent, text = nodes[pos]
        if cur_indent < indent or not text.startswith("- "):
            break
        body = text[2:].strip()
        # URL 里的冒号不是键值分隔符
        is_pair = ":" in body and not body.startswith(("http://", "https://"))
        if not is_pair:
            items.append(_parse_scalar(body))
            pos += 1
            continue

        key, _, rest = body.partition(":")
        item: dict[str, Any] = {}
        if rest.strip() == "":
            item[key.strip()], pos = _parse_child(nodes, pos + 1, cur_indent + 2)
        else:
            item[key.strip()] = _parse_scalar(rest.strip())
            pos += 1
        # 同一项里缩进更深的其余键，并入这一项
        while pos < len(nodes):
            next_indent, next_text = nodes[pos]
            if next_indent <= cur_indent or next_text.startswith("- "):
                break
            extra, pos = _parse_map(nodes, pos, next_indent)
            item.update(extra)
        items.append(item)
    return items, pos


def _tiny_yaml(path: Path) -> dict[str, Any]:
    """没有 pyyaml 时的兜底解析。

    支持本项目用到的全部结构：标量、映射、序列，以及「序列里的映射」
    （外部测试集 suites.yaml 就是这个形态）。靠缩进栈实现，不依赖任何第三方包。
    """
    try:
        nodes = _read_nodes(path)
    except OSError:
        return {}
    value, _ = _parse_block(nodes, 0, nodes[0][0] if nodes else 0)
    return value if isinstance(value, dict) else {}


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p.strip()) for p in inner.split(",")]
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def estimate_from_spec(
    requests: int,
    input_tokens: tuple[int, int],
    output_tokens: tuple[int, int],
    price: PriceTable,
    reasoning: bool = False,
) -> CostRange:
    """把一份「请求数 + token 区间」换算成钱与时间的区间。"""
    multiplier = REASONING_MULTIPLIER if reasoning else 1.0
    out_low, out_high = output_tokens

    def money(in_tok: int, out_tok: int) -> float:
        return (
            in_tok / 1_000_000 * price.input_per_mtok
            + out_tok * multiplier / 1_000_000 * price.output_per_mtok
        )

    in_low, in_high = input_tokens
    amount_low = money(in_low, int(out_low * 0.6))
    amount_high = money(in_high, int(out_high * 1.4))

    def seconds(out_tok: int) -> float:
        return requests * price.avg_seconds_per_request + out_tok * multiplier / max(
            1.0, price.tokens_per_second
        )

    return CostRange(
        requests=requests,
        input_tokens=(in_low, in_high),
        output_tokens=(out_low, out_high),
        seconds=(round(seconds(out_low) * 0.7, 1), round(seconds(out_high) * 1.5, 1)),
        amount=(round(amount_low, 6), round(amount_high, 6)),
        currency=price.currency,
    )


def combine_ranges(ranges: list[CostRange]) -> CostRange:
    if not ranges:
        return CostRange()
    currency = ranges[0].currency
    total = CostRange(
        requests=sum(r.requests for r in ranges),
        input_tokens=(
            sum(r.input_tokens[0] for r in ranges),
            sum(r.input_tokens[1] for r in ranges),
        ),
        output_tokens=(
            sum(r.output_tokens[0] for r in ranges),
            sum(r.output_tokens[1] for r in ranges),
        ),
        seconds=(sum(r.seconds[0] for r in ranges), sum(r.seconds[1] for r in ranges)),
        amount=(sum(r.amount[0] for r in ranges), sum(r.amount[1] for r in ranges)),
        currency=currency,
    )
    return total


def format_estimate(estimate: CostRange, title: str = "消耗预估") -> str:
    low, high = estimate.amount
    sec_low, sec_high = estimate.seconds
    in_low, in_high = estimate.input_tokens
    out_low, out_high = estimate.output_tokens
    symbol = {"USD": "$", "CNY": "¥"}.get(estimate.currency, "")
    return (
        f"{title}\n"
        f"  请求数      {estimate.requests}\n"
        f"  输入 token  {in_low} – {in_high}\n"
        f"  输出 token  {out_low} – {out_high}\n"
        f"  预计耗时    {sec_low:.0f}s – {sec_high:.0f}s\n"
        f"  预计费用    {symbol}{low:.4f} – {symbol}{high:.4f} {estimate.currency}"
    )


def over_budget(estimate: CostRange, max_cost: float | None, max_seconds: float | None) -> str | None:
    """开跑前就知道超预算，就别跑了。返回原因或直接 None。"""
    if max_cost is not None and estimate.amount[0] > max_cost:
        return (
            f"预估下限 {estimate.amount[0]:.4f} 已超过预算上限 {max_cost:.4f}，"
            f"建议换更轻的套件或提高上限"
        )
    if max_seconds is not None and estimate.seconds[0] > max_seconds:
        return (
            f"预估耗时下限 {estimate.seconds[0]:.0f}s 已超过时间上限 "
            f"{max_seconds:.0f}s，建议换更轻的套件"
        )
    return None
