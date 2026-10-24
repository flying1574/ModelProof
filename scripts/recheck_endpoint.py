"""重跑一次真实端点，把「为什么测不出来」打印出来。

用法：
    python scripts/recheck_endpoint.py <base_url> <model>

只做观测与诊断，不做任何写入（除了指纹台账的自动记录）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modelproof.core.orchestrator import run_verification  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base_url, model = sys.argv[1], sys.argv[2]

    print(f"端点 {base_url}  模型 {model}\n")
    api_key = sys.argv[3] if len(sys.argv) > 3 else ""
    report = run_verification(base_url, api_key, model, suite=None,
                              target_layer="version", bank="default")

    print("\n── 裁决 ──────────────────────────")
    verdict = report.verdict or {}
    print(f"  {verdict.get('state')}  （退出码 {verdict.get('exit_code')}）")
    print(f"  {verdict.get('basis')}")
    for reason in verdict.get("reasons") or []:
        print(f"    · {reason}")

    if report.multi_backend:
        block = report.multi_backend
        print("\n── 多后端分派 ────────────────────")
        print(f"  整体相对差 {block['best_diff']:.1%}（最接近 {block['best_reference']}）")
        print(f"  可解释 {block['considered'] - block['unresolved']}/{block['considered']} 格")
        print(f"  逐格投票 {block['votes']}")

    if report.leads:
        print("\n── 未确认线索 ────────────────────")
        for item in report.leads["items"]:
            print(f"  [{item['channel']}] {item['text']}")
            print(f"      {item['caveat']}")

    print("\n── 各层 ──────────────────────────")
    for layer in ("vendor", "family", "series", "version"):
        value = (report.layers or {}).get(layer)
        if not value:
            continue
        state = value.value if not value.abstained else "弃权"
        print(f"  {layer:8s} {state}  ({value.confidence:.0%}) {value.detail or ''}")

    print("\n── 失败 / 弃权的通道 ──────────────")
    for ev in report.evidence:
        if ev.weight > 0 and ev.candidates:
            continue
        print(f"  [{ev.channel}] {ev.observed}")
        if ev.note:
            print(f"      {ev.note}")

    print("\n（完整报告见导出的 Markdown / JSON）")
    return int(verdict.get("exit_code") or 0)


if __name__ == "__main__":
    sys.exit(main())
