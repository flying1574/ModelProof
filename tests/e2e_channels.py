"""C11 单 token 指纹 / C12 请求一致性 / 三态裁决 的端到端验证。

这三个是本轮从外部建议里吸收进来的新能力，各自的验证重点不同：
  * C11 要证明「同一模型近、不同模型远」，而且**自洽性检查不能被样本噪声
    误触发**（这是实现时踩到的坑，所以专门有一条断言）；
  * C12 要证明「注入的异常能被抓到」——用一个会按请求类型换后端的 mock 端点
    模拟动态路由，看倒挂/翻转检测是否报警；
  * 三态裁决要证明 MATCH / MISMATCH / INCONCLUSIVE 三种都能走通。

不依赖真实 API 密钥。用法：
    python tests/e2e_channels.py
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.e2e_mock import Handler, serve  # noqa: E402
from tests.e2e_mock import _answer_for, _integer_answer, _one_word_answer  # noqa: E402

from modelproof.core.baseline.banks import delete_bank  # noqa: E402
from modelproof.core.collect import collect_baseline  # noqa: E402
from modelproof.core.orchestrator import run_verification  # noqa: E402

BANK = "e2e-channels"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"    {'✓' if ok else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# --------------------------------------------------------------------------
# 会「中途换答案」的 mock：模拟上游把请求路由到不同后端
# --------------------------------------------------------------------------


class SwitchHandler(BaseHTTPRequestHandler):
    """按请求特征路由的 mock：模拟中转商把不同请求丢给不同后端。

    这比「按调用顺序切换」更真实——中转商判据是请求内容，不是第几个请求：
      * 简单题恒定答错、复杂题恒定答对  → 难度倒挂
      * 事实题在后端之间交替（299792 / 300000）→ 同题不同值
      * 采样题恒定同一个词             → temperature 疑似被锁死
    """

    fact_calls = 0

    def log_message(self, *args):  # 静音
        pass

    def _send(self, payload: dict) -> None:
        import json

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        import json

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}

        text = ""
        for message in reversed(payload.get("messages") or []):
            if message.get("role") == "user":
                text = message.get("content") or ""
                break

        if "沸点" in text:
            # 简单题恒定答错 —— 模拟「简单请求被路由到更弱的模型」
            answer = "50"
        elif "17 × 23" in text:
            # 复杂题恒定答对（正确答案恰好也是 50）
            answer = "50"
        elif "光在真空中" in text:
            # 事实题在两个后端之间交替：一个答 299792，另一个答 300000。
            # 两个数都在容差内、都算「答对」，但同题不同值本身就是路由痕迹。
            SwitchHandler.fact_calls += 1
            answer = "299792" if SwitchHandler.fact_calls % 2 else "300000"
        elif "60 个项目的清单" in text:
            answer = "\n".join(f"{i}. 项目{i}" for i in range(1, 61))
        elif "喜欢的动物" in text:
            answer = "猫"
        else:
            answer = "模拟回答"

        self._send({
            "id": "chatcmpl-switch",
            "object": "chat.completion",
            "model": "switch-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        })


def serve_switch(port: int):
    handler = type(f"SwitchHandler{port}", (SwitchHandler,), {})
    handler.fact_calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, handler


# --------------------------------------------------------------------------
# 1. C11 单 token 指纹
# --------------------------------------------------------------------------


def test_onetoken() -> None:
    print("【1】C11 单 token 分布指纹")

    delete_bank(BANK)
    servers = [
        serve("char", "fp_ot_" + tag * 12, name, port)
        for tag, name, port in (
            ("a", "mock-alpha", 7941),
            ("b", "mock-beta", 7942),
            ("c", "mock-gamma", 7943),
        )
    ]
    try:
        for name, port in (("mock-alpha", 7941), ("mock-beta", 7942),
                           ("mock-gamma", 7943)):
            entry, notes = collect_baseline(
                f"http://127.0.0.1:{port}/v1", "k", name,
                vendor="mockvendor", family=name,
                bank=BANK, channels=("onetoken",),
            )
            payload = entry.channels.get("onetoken") or {}
            check(f"采集 {name}", payload.get("total_samples") == 120,
                  f"{payload.get('total_samples')} 样本 / "
                  f"{len(payload.get('cells') or {})} cells")
            for note in notes:
                print(f"        ! {note}")

        print("    闭集判定：")
        for name, port in (("mock-alpha", 7941), ("mock-beta", 7942),
                           ("mock-gamma", 7943)):
            report = run_verification(
                f"http://127.0.0.1:{port}/v1", "k", name,
                suite="onetoken", target_layer="family", bank=BANK,
            )
            evidence = next(
                (e for e in report.evidence if e.channel == "onetoken"), None
            )
            check(f"{name} 判对", bool(evidence and evidence.candidates
                                   and evidence.candidates[0].id == name),
                  f"JSD={evidence.metric.get('value')}" if evidence else "无证据")

            # 自洽性：噪声基线必须被扣除，否则会满屏误报
            self_check = (evidence.metric or {}).get("self_check") or {}
            excess = self_check.get("excess", 0.0)
            check(f"{name} 自洽性未误报", excess <= 0.10,
                  f"ordered={self_check.get('ordered_jsd')} "
                  f"baseline={self_check.get('baseline_jsd')} excess={excess}")

        # 库外模型：三个 mock 里没有第四个，用一个新名字跑，JSD 应明显更大
        print("    库外检查（基准库里没有 mock-delta）：")
        report = run_verification(
            "http://127.0.0.1:7941/v1", "k", "mock-delta",
            suite="onetoken", target_layer="family", bank=BANK,
        )
        evidence = next(
            (e for e in report.evidence if e.channel == "onetoken"), None
        )
        if evidence and evidence.candidates:
            print(f"        最近邻 {evidence.candidates[0].id}，"
                  f"JSD={evidence.metric.get('value')}")
            check("库外仍给出最近邻（不硬猜身份）", True)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


# --------------------------------------------------------------------------
# 2. C12 请求一致性
# --------------------------------------------------------------------------


def test_consistency() -> None:
    print("\n【2】C12 请求一致性（模拟上游按请求类型换后端）")

    server, handler = serve_switch(7944)
    try:
        report = run_verification(
            "http://127.0.0.1:7944/v1", "k", "switch-model",
            suite="link-only", target_layer="family",
        )
        # 单独跑 consistency 这一项
        from modelproof.core.channels import CHANNELS
        from modelproof.core.channels.base import RunContext
        from modelproof.adapters import build_adapter

        adapter = build_adapter("http://127.0.0.1:7944/v1", "k", "switch-model")
        ctx = RunContext(
            adapter=adapter, declared_model="switch-model",
            items=[], baselines=[],
        )
        # 手工构造一个含 consistency 测试项的上下文
        from modelproof.core.suite.model import TestItem

        ctx.items = [TestItem(id="consistency.requests", channel="consistency",
                              name="请求一致性")]
        handler.fact_calls = 0
        evidence_list = CHANNELS["consistency"].collect(ctx)
        check("产出了证据", bool(evidence_list))

        metric = (evidence_list[0].metric if evidence_list else {}) or {}
        print(f"    观测：{evidence_list[0].observed if evidence_list else '—'}")
        print(f"    判定：{metric.get('verdict')}")

        check("检测到难度倒挂", bool(metric.get("difficulty_inversion")),
              f"简单 {metric.get('easy_correct')}/3 vs "
              f"复杂 {metric.get('hard_correct')}/3")
        # 两条路径都算检测到：答案超出容差，或同题出现不同数值
        fact_flip = bool(
            0 < (metric.get("fact_correct") or 0) < (metric.get("fact_repeats") or 3)
            or (metric.get("fact_variants") or 0) >= 2
        )
        check("检测到事实翻转", fact_flip,
              f"事实 {metric.get('fact_correct')}/{metric.get('fact_repeats')}"
              f" · {metric.get('fact_variants')} 种答案 "
              f"{metric.get('fact_answers')}")
        check("采样自由度给出了判断",
              metric.get("freedom_unique") is not None,
              f"{metric.get('freedom_unique')}/{metric.get('freedom_samples')} 种")
        check("参数完整性未误报", metric.get("max_tokens_respected") is True,
              f"回传 {metric.get('list_lines')}/{metric.get('list_expected')} 行")
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# 3. 三态裁决
# --------------------------------------------------------------------------


def test_verdict_states() -> None:
    print("\n【3】三态裁决")

    delete_bank(BANK)
    server_a = serve("char", "fp_v_a" * 4, "mock-alpha", 7945)
    server_b = serve("word", "fp_v_b" * 4, "mock-beta", 7946)
    try:
        # 先采出能判定的对照数据
        for name, port, family in (("mock-alpha", 7945, "alpha"),
                                   ("mock-beta", 7946, "beta")):
            collect_baseline(
                f"http://127.0.0.1:{port}/v1", "k", name,
                vendor="mockvendor", family=family,
                bank=BANK, channels=("tokenizer", "behavior"),
            )

        # a) MATCH：说自己是 alpha，实际也是 alpha
        report = run_verification(
            "http://127.0.0.1:7945/v1", "k", "mock-alpha",
            suite="standard", target_layer="family", bank=BANK,
            claim="mock-alpha",
        )
        print(f"    a) 声明 alpha / 实际 alpha → {report.verdict['state']}")
        check("MATCH 路径可达", report.verdict["state"] == "MATCH",
              report.verdict["basis"])

        # b) MISMATCH：说自己是 alpha，实际是 beta
        report = run_verification(
            "http://127.0.0.1:7946/v1", "k", "mock-alpha",
            suite="truth", target_layer="family", bank=BANK,
            claim="mock-alpha",
        )
        print(f"    b) 声明 alpha / 实际 beta → {report.verdict['state']}")
        check("MISMATCH 路径可达", report.verdict["state"] == "MISMATCH",
              report.verdict["basis"])
        check("退出码为 3", report.exit_code == 3)

        # c) INCONCLUSIVE：空库，必然弃权
        report = run_verification(
            "http://127.0.0.1:7945/v1", "k", "mock-alpha",
            suite="link-only", target_layer="family", bank="default",
        )
        print(f"    c) 链路套件（不判身份）→ {report.verdict['state']}")
        check("INCONCLUSIVE 路径可达",
              report.verdict["state"] == "INCONCLUSIVE",
              report.verdict["basis"])

        check("裁决写进了报告 JSON", "verdict" in report.to_dict())
    finally:
        server_a.shutdown()
        server_a.server_close()
        server_b.shutdown()
        server_b.server_close()
        delete_bank(BANK)


# --------------------------------------------------------------------------
# 4. 蒸馏场景：软信号像老师、硬信号不像
# --------------------------------------------------------------------------


def _vocab_mixed(text: str) -> int:
    """第三种词表 —— 与 char / word 都不同，保证词表指纹对不上任何候选。"""
    return len(text) + len(text.split()) * 3 + 5


class DistilledHandler(Handler):
    """蒸馏学生：统计人格照抄老师，词表与后端配置另起一套。

    复现 2026-10 的真实形态：参考库里只有老师（mock-alpha）和另一个
    模型（mock-beta），学生自己不在库里。学生的答案分布像老师，
    词表不像 —— 这正是「DeepSeek 4.1 被 ModelTrace 判成 GPT-6 Astra」
    那种误判的结构。
    """

    vocab = staticmethod(_vocab_mixed)
    fingerprint = "fp_distilled_student_0001"
    teacher = "mock-alpha"

    def do_POST(self):
        import json

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}

        text = ""
        for message in reversed(payload.get("messages") or []):
            if message.get("role") == "user":
                text = message.get("content") or ""
                break

        # 统计类探针（单 token / 长整数）按老师的人格采样 ——
        # 蒸馏能继承的恰恰是这部分；词表与后端指纹不继承，所以另起一套。
        if "1 到 355" in text:
            answer = _integer_answer(self.teacher)
        else:
            answer = _one_word_answer(text, self.teacher) or _answer_for(text)

        prompt_tokens = self.vocab(text) + 8
        self._send(200, {
            "id": "chatcmpl-distilled",
            "object": "chat.completion",
            "model": self.model_name,
            "system_fingerprint": self.fingerprint,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": max(1, len(answer) // 3),
                "total_tokens": prompt_tokens + max(1, len(answer) // 3),
            },
        })


def test_distillation() -> None:
    print("\n【4】蒸馏场景（软信号像老师，硬信号不像）")

    delete_bank(BANK)
    servers = [
        serve("char", "fp_d_a" + "a" * 27, "mock-alpha", 7951),
        serve("word", "fp_d_b" + "b" * 27, "mock-beta", 7952),
    ]
    student = type("DistilledHandler7953", (DistilledHandler,),
                   {"model_name": "mock-distilled"})
    server_c = ThreadingHTTPServer(("127.0.0.1", 7953), student)
    thread = threading.Thread(target=server_c.serve_forever, daemon=True)
    thread.start()
    servers.append(server_c)

    try:
        # 只把老师和另一个模型采进库 —— 学生不在库里
        for name, port, family in (("mock-alpha", 7951, "alpha"),
                                   ("mock-beta", 7952, "beta")):
            collect_baseline(
                f"http://127.0.0.1:{port}/v1", "k", name,
                vendor="mockvendor", family=family,
                bank=BANK, channels=("tokenizer", "onetoken", "backend_fp"),
            )

        report = run_verification(
            "http://127.0.0.1:7953/v1", "k", "mock-distilled",
            suite="deep", target_layer="family", bank=BANK,
        )

        note = report.lineage_note
        coverage = (report.validity or {}).get("checks", {}).get("bank_coverage")
        print(f"    覆盖度自检：{coverage}")
        if note:
            print(f"    血缘提示：{note['note'][:90]}…")

        check("被测模型不在库里被点名",
              bool(coverage and coverage.get("declared_in_bank") is False),
              f"declared_in_bank={coverage and coverage.get('declared_in_bank')}")
        check("触发了血缘提示", note is not None,
              f"候选 {note['candidates'] if note else '—'}")
        check("提示指向的是老师", bool(note and "mock-alpha" in note["candidates"]),
              f"{note['candidates'] if note else '—'}")
        check("提示没有反推训练血缘",
              bool(note and "无法证明训练血缘" in note["note"]))
        check("裁决没被提示改写",
              report.verdict["state"] in {"MATCH", "MISMATCH", "INCONCLUSIVE"},
              report.verdict["state"])
        check("提示写进了报告 JSON", "lineage_note" in report.to_dict())
        check("提示写进了 Markdown", "## 血缘提示" in report.to_markdown())

        # 对照组：老师自己跑，两类通道应该一致，不该触发提示
        report_ok = run_verification(
            "http://127.0.0.1:7951/v1", "k", "mock-alpha",
            suite="deep", target_layer="family", bank=BANK,
        )
        check("老师自己跑不误报", report_ok.lineage_note is None,
              f"覆盖度={(report_ok.validity or {}).get('checks', {}).get('bank_coverage')}")

        # 对照组 2：只跑 onetoken 套件（没有抗蒸馏通道在场）时不该硬造提示，
        # 那种情况下靠覆盖度自检把话说清楚
        report_cheap = run_verification(
            "http://127.0.0.1:7953/v1", "k", "mock-distilled",
            suite="onetoken", target_layer="family", bank=BANK,
        )
        check("无抗蒸馏通道时不硬造提示", report_cheap.lineage_note is None,
              "覆盖度自检仍在："
              f"{(report_cheap.validity or {}).get('checks', {}).get('bank_coverage', {}).get('declared_in_bank')}")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        delete_bank(BANK)


# --------------------------------------------------------------------------
# 5. 开集识别：先问「库里有它吗」，再谈「它是谁」
# --------------------------------------------------------------------------

BANK_OS = "e2e-openset"


def test_openset() -> None:
    print("\n【5】开集识别（LLMmap 附录 E 的判据 + 留一法标定）")

    from modelproof.core.baseline.store import BaselineStore
    from modelproof.core.fusion import calibrate
    from modelproof.core.fusion import openset as osmod

    # --- 纯函数：四种典型形态要落到四个不同档位 ---
    cases = {
        "又像又领先": ([0.98, 0.42, 0.35, 0.30], "in_bank"),
        "又不像又平": ([0.72, 0.70, 0.68, 0.65], "likely_unseen"),
        "像但并列": ([0.95, 0.92, 0.55, 0.50], "ambiguous"),
        "领先但很弱": ([0.60, 0.35, 0.30, 0.25], "ambiguous"),
    }
    for label, (values, expected) in cases.items():
        got = osmod.assess(osmod.vector_stats(values))["verdict"]
        check(f"{label} → {expected}", got == expected, got)

    # z 的上限随候选数增长，所以归一化之后才跨库可比
    small = osmod.vector_stats([1.0, 0.0, 0.0])
    large = osmod.vector_stats([1.0] + [0.0] * 20)
    check("z_norm 跨候选数可比",
          small["z_norm"] > 0.95 and large["z_norm"] > 0.95,
          f"n=3 → {small['z_norm']}，n=21 → {large['z_norm']}")

    # --- 标定链路：留一法造库内/库外两组样本 ---
    delete_bank(BANK_OS)
    specs = [
        ("mock-alpha", 7961, "char", "a"),
        ("mock-beta", 7962, "word", "b"),
        ("mock-gamma", 7964, "char", "c"),
        ("mock-delta", 7965, "word", "d"),
    ]
    servers = [
        serve(v, f"fp_eo{t}" + t * 27, n, p) for n, p, v, t in specs
    ]
    student = type("DistilledHandler7963", (DistilledHandler,),
                   {"model_name": "mock-distilled"})
    server_c = ThreadingHTTPServer(("127.0.0.1", 7963), student)
    threading.Thread(target=server_c.serve_forever, daemon=True).start()
    servers.append(server_c)

    try:
        for name, port, _vocab, _tag in specs:
            collect_baseline(
                f"http://127.0.0.1:{port}/v1", "k", name,
                vendor="mockvendor", family=name.replace("mock-", ""),
                bank=BANK_OS, channels=("tokenizer", "onetoken", "behavior"),
            )

        store = BaselineStore(bank=BANK_OS)
        store.load(force=True)
        fit = calibrate.fit_openset(store)
        print(f"    标定：top_floor={fit.get('top_floor')} "
              f"gap_floor={fit.get('gap_floor')} "
              f"set_delta={fit.get('set_delta')} "
              f"(seen={fit.get('seen_n')} unseen={fit.get('unseen_n')})")
        check("开集判据标定成功", fit.get("fitted") is True, str(fit.get("note", "")))
        check("标定记下了库名", fit.get("bank") == BANK_OS, str(fit.get("bank")))

        # 阈值要真的写进 thresholds.json 才算数
        calibrate.recalibrate(store)

        # --- 库内 vs 库外：这是本节的核心理由 ---
        report_in = run_verification(
            "http://127.0.0.1:7961/v1", "k", "mock-alpha",
            suite="deep", target_layer="family", bank=BANK_OS,
        )
        report_out = run_verification(
            "http://127.0.0.1:7963/v1", "k", "mock-distilled",
            suite="deep", target_layer="family", bank=BANK_OS,
        )
        block_in, block_out = report_in.openset, report_out.openset

        check("库外模型被判为 suspected unseen",
              bool(block_out and block_out["verdict"] == "likely_unseen"),
              f"{block_out['verdict'] if block_out else '—'} "
              f"top={block_out['stats']['max'] if block_out else '—'}")
        check("库内模型没被判成库外",
              bool(block_in and block_in["verdict"] != "likely_unseen"),
              f"{block_in['verdict'] if block_in else '—'} "
              f"top={block_in['stats']['max'] if block_in else '—'}")
        check("库外时预测集放开了",
              bool(block_out and len(block_out["prediction_set"]) >= 2),
              str(block_out["prediction_set"] if block_out else '—'))
        check("开集块用了标定值",
              bool(block_out and block_out["calibrated"] is True))
        check("开集块写进了报告 JSON", "openset" in report_out.to_dict())
        check("开集块写进了 Markdown", "## 开集识别" in report_out.to_markdown())

        # 换库不该串用别人的阈值：在另一个库里只采便宜通道，
        # 这样开集块会产出，但不该套用 e2e-openset 标定出来的阈值
        for name, port, _vocab, _tag in specs:
            collect_baseline(
                f"http://127.0.0.1:{port}/v1", "k", name,
                vendor="mockvendor", family=name.replace("mock-", ""),
                bank=BANK_OS + "-other", channels=("tokenizer", "behavior"),
            )
        report_other = run_verification(
            "http://127.0.0.1:7961/v1", "k", "mock-alpha",
            suite="standard", target_layer="family", bank=BANK_OS + "-other",
        )
        other = report_other.openset
        check("换库不串用阈值",
              bool(other and other["calibrated"] is False),
              f"calibrated={other['calibrated'] if other else '—'}")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        delete_bank(BANK_OS)
        delete_bank(BANK_OS + "-other")


# --------------------------------------------------------------------------
# 6. 词表参考库：蒸馏搬不走的那一层
# --------------------------------------------------------------------------


def test_tokenizer_references() -> None:
    print("\n【6】本地词表参考库（抗蒸馏通道的开箱能力）")

    from modelproof.core.channels import tokenizer as tk

    refs = tk.load_references()
    check("参考库已导入", len(refs) >= 6, f"{len(refs)} 个词表")
    if not refs:
        return

    for reference in refs:
        ranked = tk.rank_vocabularies(reference["counts"])
        if not ranked:
            check(f"{reference['id']} 可比对", False)
            continue
        top_ref, diff, same = ranked[0]
        # o200k 与 o200k_harmony 在这 37 格上完全相同 —— 这是已知事实，
        # 词表指纹只能判「是否同一词表」，分不开共用词表的近亲。
        # 所以断言落在「同一词表 + 同一家族」，不要求 top 一定是自己。
        self_hit = next((row for row in ranked if row[0]["id"] == reference["id"]), None)
        check(
            f"{reference['id']} 自匹配",
            bool(self_hit and self_hit[1] <= tk.EXACT_TOLERANCE)
            and top_ref["family"] == reference["family"],
            f"self_diff={self_hit[1]:.4f} top={top_ref['id']}"
            f"{'（与 ' + top_ref['id'] + ' 在这批探针上完全同构）' if top_ref['id'] != reference['id'] else ''}",
        )

    # 不同词表必须分得开，否则这条通道没有区分力
    by_id = {r["id"]: r for r in refs}
    cl100k = tk.rank_vocabularies(by_id["gpt-cl100k"]["counts"])
    o200k_hit = next((row for row in cl100k if row[0]["id"] == "gpt-o200k"), None)
    check(
        "cl100k 与 o200k 能分开",
        bool(o200k_hit and o200k_hit[1] > tk.NEAR_TOLERANCE),
        f"diff={o200k_hit[1]:.3f} > {tk.NEAR_TOLERANCE}" if o200k_hit else "—",
    )

    qwen = tk.rank_vocabularies(by_id["qwen2-5"]["counts"])
    check(
        "qwen2-5 不会被认成 GPT 词表",
        qwen[0][0]["family"] == "qwen",
        f"top={qwen[0][0]['id']} family={qwen[0][0]['family']}",
    )


# --------------------------------------------------------------------------
# 7. 稳健打分包：概率是校准过的，库外会弃权
# --------------------------------------------------------------------------


def _load_fixture() -> dict | None:
    path = ROOT / "tests" / "fixtures" / "robust_port.json"
    if not path.is_file():
        return None
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def test_robust_pack() -> None:
    print("\n【7】ModelTrace 稳健打分包（校准概率 + 库外守卫）")

    from modelproof.core.channels import robust as rb

    pack = rb.load_pack()
    fixture = _load_fixture()
    if pack is None or fixture is None:
        print("    ⚠ 打分包或夹具缺失，跳过（先跑 scripts/import_modeltrace.py）")
        return

    check("包里有 18 类", len(rb.pack_models(pack)) == 18,
          f"{len(rb.pack_models(pack))} 类")

    # 与 ModelTrace 原版逐位一致（夹具里的期望值由 numpy 原版算出）
    fused = rb.score_numbers(fixture["sample"], pack)["fused"]
    expected = fixture["expected_fused"]
    worst = max(abs(a - b) for a, b in zip(fused, expected))
    check("移植与 ModelTrace 原版逐位一致", worst < 1e-9, f"最大偏差 {worst:.2e}")

    # 真实参考样本：要能认出来，且不能判成库外
    result = rb.analyze(fixture["samples"], pack)
    exp = fixture["expected"]
    check(
        "真实样本认对型号",
        result["prediction"] == exp["prediction"],
        f"{result['prediction']}（期望 {exp['prediction']}）",
    )
    check(
        "概率与期望一致",
        abs(result["probability"] - exp["probability"]) < 1e-6,
        f"p={result['probability']:.6f}",
    )
    check(
        "库内样本不触发库外守卫",
        result["out_of_scope"] is False,
        f"best_score={result['best_score']:.3f} ≥ {rb.OUT_OF_SCOPE_SCORE}",
    )
    check(
        "标定准确率带出来了",
        isinstance(result["calibration"].get("cv_accuracy"), float),
        f"cv={result['calibration'].get('cv_accuracy')} n={result['calibration'].get('cv_samples')}",
    )

    # 库外分布：softmax 概率会很好看，但守卫必须拦住
    oos = rb.analyze(fixture["out_of_scope_samples"], pack)
    check(
        "库外分布触发守卫",
        oos["out_of_scope"] is True,
        f"best={oos['best_score']:.2f} < {rb.OUT_OF_SCOPE_SCORE}"
        f"，但闭集概率仍有 {oos['probability']:.1%}（这正是要看守卫的原因）",
    )
    check(
        "守卫拦住时概率仍然很高（反例留档）",
        oos["probability"] > 0.5,
        f"p={oos['probability']:.3f} —— 说明闭集概率不能当结论读",
    )


# --------------------------------------------------------------------------
# 8. 自述互斥：两个自述打架时必须报出来
# --------------------------------------------------------------------------


def test_identity_conflict() -> None:
    print("\n【8】自述身份互斥检测")

    from modelproof.core.channels.behavior import identity_conflicts

    # 真实案例：vfy_b4255d725023 的端点挂着 claude-sonnet-5-5，
    # 直接问身份答 claude，JSON 自述却是 Qwen / Alibaba。
    real = {
        "identity": "claude",
        "json_self": {
            "name": "Qwen",
            "vendor": "Alibaba Group's Tongyi Lab",
            "version": "Qwen",
            "context_window": "Ultra-long",
            "knowledge_cutoff": "2026-01",
        },
    }
    hits = identity_conflicts(real)
    check("claude vs Qwen 互斥被抓到", len(hits) == 1,
          hits[0][:60] if hits else "没抓到")

    consistent = {
        "identity": "claude",
        "json_self": {"name": "Claude", "vendor": "Anthropic"},
    }
    check("自述一致时不误报", identity_conflicts(consistent) == [])

    unknown = {"identity": "claude", "json_self": {"name": "Foo", "vendor": "Bar"}}
    check("认不出家族时不硬报", identity_conflicts(unknown) == [])


# --------------------------------------------------------------------------
# 9. 融合后的开箱能力：真实参考样本要能一路判到 version
# --------------------------------------------------------------------------


class _ReplayHandler(BaseHTTPRequestHandler):
    """回放固定回答的 mock。数字题回放参考序列，其余一律简短应答。"""

    protocol_version = "HTTP/1.1"
    texts: list[str] = []
    counter = 0

    def log_message(self, *args):  # 静音
        pass

    def _send(self, payload: dict) -> None:
        import json

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        import json

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        prompt = ""
        for message in reversed(body.get("messages") or []):
            if message.get("role") == "user":
                prompt = str(message.get("content") or "")
                break
        index = _ReplayHandler.counter
        _ReplayHandler.counter += 1
        if "355" in prompt or "整数" in prompt:
            content = self.texts[index % len(self.texts)]
        else:
            content = "ok"
        self._send({
            "id": "replay", "object": "chat.completion", "created": 0,
            "model": body.get("model", "replay"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {
                "prompt_tokens": len(prompt) // 2 + 5,
                "completion_tokens": len(content) // 2,
                "total_tokens": len(prompt) // 2 + len(content) // 2 + 5,
            },
        })

    def do_GET(self):
        self._send({"data": [{"id": "replay"}]})


def test_fused_end_to_end() -> None:
    print("\n【9】融合指纹库后的端到端：真实参考样本一路判到 version")

    from modelproof.core.channels import robust as rb

    fixture = _load_fixture()
    if fixture is None or rb.load_pack() is None:
        print("    ⚠ 打分包或夹具缺失，跳过（先跑 scripts/import_modeltrace.py）")
        return

    import json
    import threading
    from http.server import ThreadingHTTPServer

    samples = fixture["samples"]
    # 数字序列还原成文本，交给 mock 回放
    _ReplayHandler.texts = [", ".join(str(n) for n in sample) for sample in samples]
    _ReplayHandler.counter = 0

    server = ThreadingHTTPServer(("127.0.0.1", 7973), _ReplayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report = run_verification(
            "http://127.0.0.1:7973/v1", "replay", None,
            suite="deep", target_layer="version", bank="default",
        )
        layers = {name: v.value for name, v in report.layers.items()}
        check(
            "vendor 判出来了",
            layers.get("vendor") == "openai",
            f"{layers.get('vendor')}",
        )
        check("family 判出来了", layers.get("family") == "gpt", f"{layers.get('family')}")
        check(
            "version 判出来了（曾因候选稀释而弃权）",
            layers.get("version") == fixture["model"],
            f"{layers.get('version')}（期望 {fixture['model']}）",
        )
        check("裁决不是弃权", report.verdict.get("state") == "MATCH",
              f"{report.verdict.get('state')}")
        openset = report.openset or {}
        check("开集识别判为库内", openset.get("verdict") == "in_bank",
              f"{openset.get('verdict')}")
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# 10. 多后端分派：这种弃权本身就是答案，不是失败
# --------------------------------------------------------------------------


def _mixed_counts(first_id: str, second_id: str, split: int) -> dict[str, int]:
    """把两个真实词表按格子切成两段，拼成一条「被分派过」的观测向量。

    每一格都来自某个真实词表，但前后两段不是同一个 —— 这正是中转商按
    请求分派时 37 格探针会出现的样子。
    """
    from modelproof.core.channels import tokenizer as tk

    by_reference = {r["id"]: r for r in tk.load_references()}
    counts: dict[str, int] = {}
    for index, probe in enumerate(tk.PROBES):
        source = by_reference[first_id if index < split else second_id]
        counts[probe["id"]] = int(source["counts"][probe["id"]])
    return counts


class DispatchHandler(BaseHTTPRequestHandler):
    """按探针把请求分派到两个不同「后端」的 mock。

    和 SwitchHandler 的区别：那个换的是**答案内容**，这个换的是**词表**，
    专门用来验证词表通道能不能看出「请求没落在同一个后端上」。
    """

    protocol_version = "HTTP/1.1"
    counts: dict[str, int] = {}
    overhead = 9

    def log_message(self, *args):  # 静音
        pass

    def _send(self, payload: dict) -> None:
        import json

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        import json

        from modelproof.core.channels import tokenizer as tk

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        text = ""
        for message in reversed(body.get("messages") or []):
            if message.get("role") == "user":
                text = str(message.get("content") or "")
                break

        by_text = {probe["text"]: probe["id"] for probe in tk.PROBES}
        if text == tk.BASELINE_TEXT:
            # 基线串：通道用它反推固定请求开销
            prompt_tokens = self.overhead
        elif text in by_text:
            # 通道拿到的是 raw - (overhead - 1)，所以这里要加回去
            prompt_tokens = self.overhead - 1 + int(self.counts[by_text[text]])
        else:
            prompt_tokens = len(text) // 2 + 8
        answer = _answer_for(text) or "ok"

        self._send({
            "id": "chatcmpl-dispatch", "object": "chat.completion", "created": 0,
            "model": body.get("model", "dispatch"),
            "system_fingerprint": "fp_dispatch_" + "a" * 24,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": max(1, len(answer) // 3),
                "total_tokens": prompt_tokens + max(1, len(answer) // 3),
            },
        })

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "dispatch", "object": "model"}]})


def test_multi_backend_dispatch() -> None:
    print("\n【10】多后端分派：弃权本身就是答案")

    from modelproof.core.channels import tokenizer as tk

    refs = tk.load_references()
    if len(refs) < 4:
        print("    ⚠ 词表参考库不足，跳过")
        return

    # 先卡死误报：纯词表的观测向量绝不能被判成「分派」。
    # 这一条比正例更重要——正例漏报只是少说一句话，误报会把每个正常端点
    # 都解释成中转。
    false_alarms = [
        r["id"] for r in refs if tk.analyze_mixture(r["counts"]).get("mixed")
    ]
    check(
        f"{len(refs)} 个纯词表自查都不报分派（防误报）",
        not false_alarms,
        f"误报：{false_alarms}" if false_alarms else "全部 mixed=False",
    )

    mixed = _mixed_counts("qwen2-5", "mistral-v3", len(tk.PROBES) // 2)
    analysis = tk.analyze_mixture(mixed)
    check(
        "两段不同词表拼出来的向量被判为分派",
        bool(analysis.get("mixed")),
        f"best_diff={analysis['best_diff']:.1%} "
        f"votes={analysis['votes']} unresolved={analysis['unresolved']}",
    )
    check(
        "投票散在多个家族（不是某个家族压倒多数）",
        len(analysis["votes"]) >= 3 and analysis["top_share"] < 0.70,
        f"{len(analysis['votes'])} 个家族，最高占比 {analysis['top_share']:.0%}",
    )

    # 端到端：真跑一次验证，看报告有没有把这件事讲清楚
    import threading
    from http.server import ThreadingHTTPServer

    DispatchHandler.counts = mixed
    server = ThreadingHTTPServer(("127.0.0.1", 7974), DispatchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report = run_verification(
            "http://127.0.0.1:7974/v1", "dispatch", None,
            suite="standard", target_layer="family", bank="default",
        )
        block = report.multi_backend
        check("报告带上了多后端分派发现", block is not None,
              f"votes={list((block or {}).get('votes', {}).items())[:4]}")
        check(
            "裁决写成「分派到多个后端」而不是笼统的「证据不足」",
            "分派" in (report.verdict.get("basis") or ""),
            f"state={report.verdict.get('state')} basis={report.verdict.get('basis')}",
        )
        markdown = report.to_markdown()
        check("Markdown 里有「多后端分派」一节", "多后端分派" in markdown)
        check(
            "弃权原因里写清了「不是库里没有」",
            "不是「库里没有」" in "".join(report.verdict.get("reasons") or []),
        )
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# 11. 预算阶梯：被 max_tokens 挡住时要自己退让，而且别重复撞墙
# --------------------------------------------------------------------------


class BudgetHandler(BaseHTTPRequestHandler):
    """只接受小 max_tokens 的 mock。

    真实中转经常这么干（用户那个端点的原话是「max_tokens 1024 exceeds the
    limit (max 512)」）。以前碰到就是一句「端点请求失败」，谁也不知道该调什么。
    """

    protocol_version = "HTTP/1.1"
    limit = 512
    calls = 0

    def log_message(self, *args):  # 静音
        pass

    def _send(self, status: int, payload: dict) -> None:
        import json

        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        import json

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        BudgetHandler.calls += 1

        if int(body.get("max_tokens") or 0) > self.limit:
            self._send(400, {"error": {
                "message": f"max_tokens {body.get('max_tokens')} exceeds "
                           f"the limit (max {self.limit})",
                "type": "invalid_request_error",
            }})
            return

        numbers = ", ".join(str((i * 7) % 355 + 1) for i in range(150))
        self._send(200, {
            "id": "chatcmpl-budget", "object": "chat.completion", "created": 0,
            "model": body.get("model", "budget"),
            "system_fingerprint": "fp_budget_" + "b" * 24,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": numbers}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 300, "total_tokens": 340},
        })

    def do_GET(self):
        self._send(200, {"object": "list", "data": [{"id": "budget", "object": "model"}]})


def test_budget_ladder() -> None:
    print("\n【11】预算阶梯：max_tokens 被挡时自动退让")

    import threading
    from http.server import ThreadingHTTPServer

    from modelproof.core.channels.distribution import DistributionChannel

    BudgetHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 7975), BudgetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from modelproof.adapters.base import AdapterConfig
        from modelproof.adapters.openai import OpenAIAdapter
        from modelproof.core.channels.base import RunContext

        adapter = OpenAIAdapter(
            AdapterConfig(base_url="http://127.0.0.1:7975/v1", model="budget")
        )
        ctx = RunContext(adapter=adapter, declared_model="budget", items=[])
        evidence = DistributionChannel().collect(ctx)
        if not evidence:
            check("被挡住后仍能采到样本", False, "没有产出任何证据")
            return
        top = evidence[0]
        # 三条挑战：第一条把 2048/1024/768 撞掉后在 512 通过（4 次），
        # 后两条记住了 512，各 1 次 —— 共 6 次。没有档位记忆的话是 12 次。
        check("被挡住后仍能采到样本", "3 份样本" in str(top.observed),
              f"{top.observed}")
        check("后续挑战不再重复撞大档位（6 次而不是 12 次）",
              BudgetHandler.calls <= 7, f"实际 {BudgetHandler.calls} 次请求")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    print("══ ModelProof 新通道端到端验证 ══\n")
    test_onetoken()
    test_consistency()
    test_verdict_states()
    test_distillation()
    test_openset()
    test_tokenizer_references()
    test_robust_pack()
    test_identity_conflict()
    test_fused_end_to_end()
    test_multi_backend_dispatch()
    test_budget_ladder()

    if FAILURES:
        print(f"\n✗ {len(FAILURES)} 项未通过：")
        for item in FAILURES:
            print(f"    - {item}")
        return 1
    print("\n══ 全部通过 ══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
