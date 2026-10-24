"""ModelProof CLI。

退出码：0 判定成功 · 1 运行失败 · 2 弃权 · 3 真伪不符（CI 里用于阻断）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ..core.baseline.schema import BaselineEntry, validate
from ..core.baseline.store import BaselineStore
from ..core.estimate import format_estimate, load_price_table
from ..core.fusion import recalibrate
from ..core.orchestrator import (
    emit_prompts,
    estimate_run,
    run_verification,
    verify_from_answers,
)
from ..core.report import VerificationReport
from ..core.suite.registry import REGISTRY
from ..core.types import CostRange, TargetLayer

REPORTS_DIR = Path.home() / ".modelproof" / "reports"


@click.group()
@click.version_option(package_name="modelproof", prog_name="modelproof")
def cli() -> None:
    """ModelProof —— 用可验真数据鉴别与验证大模型身份。"""


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@cli.command()
@click.argument("base_url", required=False)
@click.option("--model", "-m", default="", help="端点上要测的模型 ID")
@click.option("--api-key", "-k", default="", envvar="MODELPROOF_API_KEY",
              help="API 密钥；也可用环境变量 MODELPROOF_API_KEY")
@click.option("--suite", "-s", default=None, help="测试集：quick|standard|deep|truth|link-only")
@click.option("--items", default=None, help="自由组合：逗号分隔的测试项 ID 或通道名")
@click.option("--claim", default=None, help="真伪模式：它声称自己是谁")
@click.option("--target", "target_layer", default="family",
              type=click.Choice(["vendor", "family", "series", "version"]),
              help="要判到哪一层")
@click.option("--max-cost", type=float, default=None, help="费用上限（USD）")
@click.option("--max-seconds", type=float, default=None, help="时间上限（秒）")
@click.option("--price-table", default="openai", help="价格表名")
@click.option("--protocol", default=None, type=click.Choice(["openai", "anthropic"]),
              help="强制指定协议，默认自动探测")
@click.option("--bank", default=None, help="用哪个基准库做对照（默认 default）")
@click.option("--candidates", default=None,
              help="只跟库里这几个模型比（逗号分隔；不选 = 整库）")
@click.option("--reasoning", is_flag=True, help="目标为推理模型（输出 token 预估 ×3.5）")
@click.option("--from-file", "from_file", default=None,
              help="手动模式：读入外部收集的回答（jsonl）")
@click.option("--dry-run", is_flag=True, help="只做预估，不发请求")
@click.option("--format", "output_format", default="text",
              type=click.Choice(["text", "json", "md"]))
@click.option("--save", is_flag=True, help="把报告存到 ~/.modelproof/reports")
def verify(
    base_url: str | None,
    model: str,
    api_key: str,
    suite: str | None,
    items: str | None,
    claim: str | None,
    target_layer: TargetLayer,
    max_cost: float | None,
    max_seconds: float | None,
    price_table: str,
    protocol: str | None,
    bank: str | None,
    candidates: str | None,
    reasoning: bool,
    from_file: str | None,
    dry_run: bool,
    output_format: str,
    save: bool,
) -> None:
    """验证一个端点背后的模型身份。"""
    # 手动模式
    if from_file:
        answers = _read_jsonl(from_file)
        if not answers:
            click.echo(f"✗ {from_file} 里没有读到有效回答", err=True)
            sys.exit(1)
        report = verify_from_answers(
            answers, suite=suite or "standard", claim=claim, target_layer=target_layer
        )
        _emit(report, output_format, save)
        sys.exit(report.exit_code)

    if not base_url:
        click.echo("✗ 缺少 BASE_URL（手动模式请用 --from-file）", err=True)
        sys.exit(1)

    def progress(message: str, done: int, total: int) -> None:
        if total:
            click.echo(f"  … {message} ({done}/{total})", err=True)
        else:
            click.echo(f"  … {message}", err=True)

    from ..core.progress import legacy_adapter

    try:
        report = run_verification(
            base_url,
            api_key,
            model,
            suite=suite,
            items=items,
            claim=claim,
            target_layer=target_layer,
            max_cost=max_cost,
            max_seconds=max_seconds,
            price_table=price_table,
            protocol=protocol,
            bank=bank,
            include=[c.strip() for c in candidates.split(",")] if candidates else None,
            reasoning=reasoning,
            # CLI 用的是旧的三参数回调，这里适配成结构化 Progress
            on_progress=legacy_adapter(progress),
            dry_run=dry_run,
        )
    except Exception as error:  # noqa: BLE001
        click.echo(f"✗ 运行失败：{error}", err=True)
        sys.exit(1)

    _emit(report, output_format, save)
    sys.exit(report.exit_code)


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    target = Path(path)
    if not target.exists():
        return []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _emit(report: VerificationReport, output_format: str, save: bool) -> None:
    if output_format == "json":
        click.echo(report.to_json())
    elif output_format == "md":
        click.echo(report.to_markdown())
    else:
        click.echo(_render_text(report))

    if save:
        path = report.save(REPORTS_DIR)
        click.echo(f"\n报告已保存：{path}", err=True)


def _render_text(report: VerificationReport) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("══ ModelProof 验证报告 ══")
    lines.append(f"  run_id  {report.run_id}")
    if report.input.get("declared_model"):
        lines.append(f"  模型    {report.input['declared_model']}")
    if report.input.get("base_url"):
        lines.append(f"  端点    {report.input['base_url']}")
    lines.append("")

    lines.append("── 裁决 ──")
    state = report.verdict
    mark = {"MATCH": "✓", "MISMATCH": "✗", "INCONCLUSIVE": "?"}.get(state["state"], "·")
    lines.append(f"  {mark} {state['state']}  {state['basis']}")
    for reason in state.get("reasons", []):
        lines.append(f"      - {reason}")
    lines.append("")

    lines.append("── 结论 ──")
    for layer, verdict in report.layers.items():
        if verdict.abstained:
            mark = f"弃权（{verdict.abstain_reason}）"
            value = "—"
        else:
            mark = "判定"
            value = verdict.value or "—"
        lines.append(f"  {layer:<8} {value:<24} {verdict.confidence:>6.1%}  {mark}")
        if verdict.detail:
            lines.append(f"           {verdict.detail}")
    lines.append("")

    if report.gateway:
        gw = report.gateway
        lines.append("── 接入链路 ──")
        lines.append(
            f"  {gw.get('label', gw.get('verdict'))}"
            f"（{gw.get('confidence', 0):.0%}）· {gw.get('host', '')}"
        )
        if gw.get("reason"):
            lines.append(f"  {gw['reason']}")
        for sig in gw.get("behaviour_signals", [])[:5]:
            lines.append(f"    ⚠ {sig['text']}")
        lines.append("")

    if report.truthfulness and report.truthfulness.verdict != "unknown":
        truth = report.truthfulness
        lines.append("── 真伪 ──")
        lines.append(f"  声明 {truth.claim} → {truth.verdict}（{truth.confidence:.0%}）")
        for check in truth.checks:
            flag = {True: "✓", False: "✗", None: "?"}.get(check.match, "?")
            lines.append(
                f"    {flag} {check.field}: 声明 {check.claimed} / 观测 {check.observed}"
            )
        if truth.note:
            lines.append(f"    {truth.note}")
        lines.append("")

    lines.append("── 证据 ──")
    for item in report.evidence:
        flag = {True: "✓", False: "✗", None: "·"}.get(item.match, "·")
        lines.append(f"  {flag} [{item.channel}] {item.observed}")
        if item.note:
            lines.append(f"      {item.note}")
    lines.append("")

    for conflict in report.conflicts:
        lines.append(f"  ⚠ 冲突（{conflict.layer}）：{' vs '.join(conflict.values)}")
        lines.append(f"      {conflict.hint}")

    if report.warnings:
        lines.append("")
        lines.append("── 提示 ──")
        for warning in report.warnings:
            lines.append(f"  ! {warning}")

    cost = report.cost
    if cost:
        lines.append("")
        lines.append("── 消耗 ──")
        estimate = cost.get("estimate_at_start") or {}
        if estimate:
            lo, hi = estimate.get("amount_range", [0, 0])
            lines.append(f"  预估费用 {lo:.4f} – {hi:.4f}")
        lines.append(
            f"  实际   请求 {cost.get('requests', 0)} · "
            f"{cost.get('elapsed_seconds', 0):.1f}s · "
            f"{cost.get('amount', 0):.4f} {cost.get('currency', 'USD')}"
        )

    lines.append("")
    lines.append(f"  退出码 {report.exit_code}（0 成功 / 2 弃权 / 3 真伪不符）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# estimate
# --------------------------------------------------------------------------


@cli.command()
@click.option("--suite", "-s", default="standard")
@click.option("--items", default=None)
@click.option("--model", "-m", default=None)
@click.option("--price-table", default="openai")
@click.option("--reasoning", is_flag=True)
def estimate(
    suite: str, items: str | None, model: str | None, price_table: str, reasoning: bool
) -> None:
    """开跑前的消耗预估（不发任何请求）。"""
    picked = REGISTRY.resolve(items or suite)
    result = estimate_run(picked, price_table, model, reasoning=reasoning)
    click.echo(format_estimate(result, title=f"消耗预估 · {items or suite}"))
    click.echo("")
    click.echo("  明细：")
    for item in picked:
        click.echo(
            f"    {item.id:<28} {item.requests:>3} 请求 "
            f"| 入 {item.input_tokens_range[0]}–{item.input_tokens_range[1]} "
            f"| 出 {item.output_tokens_range[0]}–{item.output_tokens_range[1]}"
        )


# --------------------------------------------------------------------------
# suite
# --------------------------------------------------------------------------


@cli.group()
def suite() -> None:
    """测试集与测试项管理。"""


@suite.command("list")
def suite_list() -> None:
    click.echo("内置测试集：")
    for entry in REGISTRY.suites():
        click.echo(f"  {entry.id:<12} {entry.name:<8} {entry.description}")
    click.echo("")
    click.echo("测试项：")
    for item in REGISTRY.items():
        click.echo(
            f"  {item.id:<28} {item.channel:<12} {item.requests:>3} 请求 "
            f"→ {','.join(item.yields) or '—'}"
        )


@suite.command("emit")
@click.option("--suite", "-s", "suite_id", default="standard")
@click.option("--format", "fmt", default="prompts",
              type=click.Choice(["prompts", "jsonl"]))
def suite_emit(suite_id: str, fmt: str) -> None:
    """手动模式：导出提示词，自己去问，再把回答喂回来。"""
    prompts = emit_prompts(suite_id)
    if fmt == "jsonl":
        for row in prompts:
            click.echo(json.dumps(row, ensure_ascii=False))
    else:
        click.echo(f"# ModelProof 手动测试 · 套件 {suite_id}")
        click.echo(f"# 共 {len(prompts)} 条。把每条的 prompt 发给目标模型，")
        click.echo("# 收集回答后按 jsonl 格式回填：")
        click.echo('# {"item": "...", "probe": "...", "answer": "..."}')
        click.echo("")
        for index, row in enumerate(prompts, start=1):
            click.echo(f"[{index}] {row['label']}（{row['item']} / {row['probe']}）")
            click.echo(row["prompt"])
            click.echo("")


# --------------------------------------------------------------------------
# baseline
# --------------------------------------------------------------------------


@cli.group()
def baseline() -> None:
    """基准（对照组）数据管理。"""


@baseline.command("list")
@click.option("--vendor", default=None)
@click.option("--family", default=None)
@click.option("--bank", default=None, help="基准库（默认 default）")
def baseline_list(vendor: str | None, family: str | None, bank: str | None) -> None:
    store = BaselineStore(bank=bank)
    entries = store.search(vendor=vendor, family=family)
    if not entries:
        click.echo("（基准库为空）")
        return
    click.echo(f"{'model_id':<32} {'vendor':<12} {'family':<10} {'series':<10} {'溯源':<6} 通道")
    for entry in sorted(entries, key=lambda e: (e.identity.vendor, e.model_id)):
        channels = ",".join(
            k for k, v in entry.channels.items() if v and v.get("enabled", True)
        )
        click.echo(
            f"{entry.model_id:<32} {entry.identity.vendor:<12} "
            f"{entry.identity.family:<10} {entry.identity.series:<10} "
            f"{entry.provenance.tier:<6} {channels or '—'}"
        )
    click.echo("")
    stats = store.stats()
    click.echo(f"共 {stats['total']} 条 · 按厂商 {stats['by_vendor']} · 按溯源 {stats['by_tier']}")


@baseline.command("show")
@click.argument("model_id")
@click.option("--bank", default=None, help="基准库（默认 default）")
def baseline_show(model_id: str, bank: str | None) -> None:
    store = BaselineStore(bank=bank)
    entry = store.get(model_id)
    if entry is None:
        click.echo(f"✗ 找不到 {model_id}", err=True)
        sys.exit(1)
    click.echo(entry.to_json())


@baseline.command("stats")
@click.option("--bank", default=None, help="基准库（默认 default）")
def baseline_stats(bank: str | None) -> None:
    store = BaselineStore(bank=bank)
    click.echo(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    if store.errors:
        click.echo("\n加载错误：")
        for error in store.errors:
            click.echo(f"  {error}")


@baseline.command("banks")
def baseline_banks() -> None:
    """列出所有基准库。"""
    from ..core.baseline.banks import list_banks

    rows = list_banks()
    if not rows:
        click.echo("（还没有任何基准库）")
        return
    click.echo(f"{'id':<20} {'名称':<16} {'条目':>6}  说明")
    for row in rows:
        click.echo(
            f"{row['id']:<20} {row['name']:<16} {row['model_count']:>6}  {row['description']}"
        )


@baseline.command("bank-create")
@click.argument("bank_id")
@click.option("--name", default="", help="显示名")
@click.option("--description", default="", help="说明")
def baseline_bank_create(bank_id: str, name: str, description: str) -> None:
    """创建一个自定义基准库。"""
    from ..core.baseline.banks import create_bank

    path = create_bank(bank_id, name, description)
    click.echo(f"✓ 已创建 {bank_id} → {path}")


@baseline.command("bank-delete")
@click.argument("bank_id")
def baseline_bank_delete(bank_id: str) -> None:
    """删除一个自定义基准库（默认库受保护）。"""
    from ..core.baseline.banks import delete_bank

    if not delete_bank(bank_id):
        click.echo(f"✗ 无法删除 {bank_id}（默认库受保护或不存在）", err=True)
        sys.exit(1)
    click.echo(f"✓ 已删除 {bank_id}")


@baseline.command("history")
@click.option("--kind", default=None, type=click.Choice(["verification", "collection"]))
@click.option("--limit", default=20, show_default=True)
def baseline_history(kind: str | None, limit: int) -> None:
    """测试历史：时间 / 被测模型 / 基准库 / 结论。"""
    from ..core.tasks import TaskStore, summarize

    rows = [summarize(t) for t in TaskStore().list(kind=kind, limit=limit)]
    if not rows:
        click.echo("（还没有任何记录）")
        return
    click.echo(f"{'时间':<20} {'模型':<24} {'库':<12} {'结论':<18} 状态")
    for row in rows:
        click.echo(
            f"{(row['created_at'] or '').replace('T', ' ').replace('Z', ''):<20} "
            f"{row['model'][:23]:<24} {row['bank'][:11]:<12} "
            f"{(row['conclusion'] or '—')[:17]:<18} {row['status']}"
        )
        click.echo(f"    {row['id']}")


@baseline.command("history-export")
@click.argument("task_id")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "md"]))
@click.option("--out", default=None, help="输出路径（默认 ~/.modelproof/reports）")
def baseline_history_export(task_id: str, fmt: str, out: str | None) -> None:
    """导出某次任务的报告。"""
    from ..core.tasks import TaskStore, export_report

    store = TaskStore()
    task = store.get(task_id)
    if task is None:
        click.echo(f"✗ 找不到任务 {task_id}", err=True)
        sys.exit(1)
    if not task.get("report"):
        click.echo("✗ 该任务没有可导出的报告", err=True)
        sys.exit(1)

    target = Path(out) if out else None
    path = export_report(task, target)
    if fmt == "md":
        report = VerificationReport.from_dict(task["report"])
        md_path = path.with_suffix(".md")
        md_path.write_text(report.to_markdown(), encoding="utf-8")
        path = md_path
    click.echo(f"✓ 已导出 {path}")


@baseline.command("validate")
@click.argument("paths", nargs=-1)
def baseline_validate(paths: tuple[str, ...]) -> None:
    """校验基准条目：schema、probe_version、溯源、内部一致性。"""
    targets = [Path(p) for p in paths]
    if not targets:
        store = BaselineStore()
        store.load(force=True)
        report = store.validate_all()
        if not report:
            click.echo("✓ 全部通过")
            return
        for model_id, problems in report:
            click.echo(f"✗ {model_id}")
            for problem in problems:
                click.echo(f"    {problem}")
        sys.exit(1 if report else 0)

    failed = 0
    for path in targets:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            click.echo(f"✗ {path}: 解析失败 {error}")
            failed += 1
            continue
        entry = BaselineEntry.from_dict(raw)
        problems = validate(entry)
        if problems:
            click.echo(f"✗ {path.name}")
            for problem in problems:
                click.echo(f"    {problem}")
            failed += 1
        else:
            click.echo(f"✓ {path.name}")
    sys.exit(1 if failed else 0)


@baseline.command("import")
@click.argument("base_url")
@click.option("--model", "-m", required=True)
@click.option("--api-key", "-k", default="", envvar="MODELPROOF_API_KEY")
@click.option("--vendor", required=True)
@click.option("--family", required=True)
@click.option("--series", default="")
@click.option("--version", default="")
@click.option("--tier", default="community",
              type=click.Choice(["official", "community", "derived"]))
@click.option("--bank", default=None, help="写入哪个基准库（默认 default，不存在则创建）")
@click.option("--channels", default="tokenizer,behavior,backend_fp",
              help="要采的通道，逗号分隔。可选："
                   "tokenizer,behavior,onetoken,distribution,capability,"
                   "style,trace,backend_fp")
@click.option("--aliases", default="", help="别名，逗号分隔")
@click.option("--note", default="", help="备注，会写进溯源")
@click.option("--dry-run", is_flag=True, help="只打印要发多少请求，不真的发")
def baseline_import(
    base_url: str,
    model: str,
    api_key: str,
    vendor: str,
    family: str,
    series: str,
    version: str,
    tier: str,
    bank: str | None,
    channels: str,
    aliases: str,
    note: str,
    dry_run: bool,
) -> None:
    """从端点自采基准数据。要求：你有权访问该端点且知道它的真实身份。

    与 Web 的「采集基准」共用 core/collect.py，两条路采出来的数据结构一致。
    """
    from ..core.baseline.banks import create_bank
    from ..core.collect import collect_baseline, describe_plan
    from ..core.progress import legacy_adapter

    wanted = tuple(c.strip() for c in channels.split(",") if c.strip())

    if dry_run:
        plan = describe_plan(wanted)
        click.echo(f"将要发送 {plan['requests']} 次请求：")
        for name, count in plan["per_channel"].items():
            click.echo(f"    {name:<14} {count}")
        return

    def progress(message: str, done: int, total: int) -> None:
        if total:
            click.echo(f"  … {message} ({done}/{total})", err=True)
        else:
            click.echo(f"  … {message}", err=True)

    target_bank = bank or "default"
    create_bank(target_bank)

    entry, notes = collect_baseline(
        base_url, api_key, model,
        vendor=vendor, family=family, series=series, version=version,
        tier=tier, bank=target_bank, channels=wanted,
        aliases=[a.strip() for a in aliases.split(",") if a.strip()],
        note=note,
        on_progress=legacy_adapter(progress),
    )

    problems = validate(entry)
    click.echo(f"✓ 已写入库 {target_bank}：{entry.model_id}")
    click.echo(f"    通道：{', '.join(sorted(entry.channels)) or '—'}")
    for item in notes:
        click.echo(f"    ! {item}")
    if problems:
        click.echo("  校验提示：")
        for problem in problems:
            click.echo(f"    {problem}")


@baseline.command("recalibrate")
@click.option("--bank", default=None, help="基准库（默认 default）")
def baseline_recalibrate(bank: str | None) -> None:
    """用留一法重算通道可靠性权重与阈值。"""
    store = BaselineStore(bank=bank)
    result = recalibrate(store)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


@cli.group()
def report() -> None:
    """历史报告查看与对比。"""


@report.command("show")
@click.argument("run_id")
@click.option("--format", "fmt", default="text", type=click.Choice(["text", "json", "md"]))
def report_show(run_id: str, fmt: str) -> None:
    path = _find_report(run_id)
    if path is None:
        click.echo(f"✗ 找不到报告 {run_id}", err=True)
        sys.exit(1)
    loaded = VerificationReport.load(path)
    if fmt == "json":
        click.echo(loaded.to_json())
    elif fmt == "md":
        click.echo(loaded.to_markdown())
    else:
        click.echo(_render_text(loaded))


@report.command("diff")
@click.argument("run_a")
@click.argument("run_b")
def report_diff(run_a: str, run_b: str) -> None:
    """对比两次运行 —— 看上游是不是换了模型或部署。"""
    path_a, path_b = _find_report(run_a), _find_report(run_b)
    if not path_a or not path_b:
        click.echo("✗ 找不到指定的报告", err=True)
        sys.exit(1)
    a, b = VerificationReport.load(path_a), VerificationReport.load(path_b)

    click.echo(f"A: {a.run_id}  {a.started_at}")
    click.echo(f"B: {b.run_id}  {b.started_at}")
    click.echo("")
    for layer in ("vendor", "family", "series", "version"):
        va, vb = a.layers.get(layer), b.layers.get(layer)
        if not va or not vb:
            continue
        same = va.value == vb.value and va.abstained == vb.abstained
        mark = "  " if same else "≠ "
        click.echo(
            f"{mark}{layer:<8} {va.value or '—'} → {vb.value or '—'}"
            f"   ({va.confidence:.0%} → {vb.confidence:.0%})"
        )

    fa = {e.channel: e.metric.get("fingerprint") for e in a.evidence}
    fb = {e.channel: e.metric.get("fingerprint") for e in b.evidence}
    for channel in set(fa) | set(fb):
        if fa.get(channel) != fb.get(channel) and (fa.get(channel) or fb.get(channel)):
            click.echo(f"≠ 后端指纹变化：{fa.get(channel)} → {fb.get(channel)}")


def _find_report(run_id: str) -> Path | None:
    if not REPORTS_DIR.is_dir():
        return None
    direct = REPORTS_DIR / f"{run_id}.json"
    if direct.exists():
        return direct
    matches = sorted(REPORTS_DIR.glob(f"{run_id}*.json"))
    return matches[0] if matches else None


if __name__ == "__main__":
    cli()
