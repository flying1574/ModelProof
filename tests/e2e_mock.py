"""端到端验证：起 mock 端点 → 采集基准 → 闭集判定。

不依赖任何真实 API 密钥，可在 CI 里直接跑。它验证的是整条链路：
适配器 → 通道采集 → 基准存储 → 融合判定 → 报告。

用法：
    python tests/e2e_mock.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 本机 mock 端点一律直连。环境里若配了 http_proxy（沙箱 / 公司网络常见），
# urllib 会把 127.0.0.1 的请求也送进代理，表现为随机挂起或 502 —— 那是测试
# 基础设施的问题，不是被测代码的问题。
for _var in ("no_proxy", "NO_PROXY"):
    os.environ.setdefault(_var, "127.0.0.1,localhost,::1")

from modelproof.core.baseline.store import BaselineStore  # noqa: E402
from modelproof.core.orchestrator import run_verification  # noqa: E402

# --------------------------------------------------------------------------
# 模拟词表：两种不同的切分策略，用来验证通道有没有区分力
# --------------------------------------------------------------------------


def vocab_char_heavy(text: str) -> int:
    """字符级：每个可见字符一个 token（模拟东亚友好度差的词表）。"""
    return max(1, len([c for c in text if not c.isspace()]))


def vocab_word_friendly(text: str) -> int:
    """词级：中文 1 字 1 token，英文按词（模拟东亚友好的词表）。"""
    tokens = 0
    buffer_is_ascii = False
    for ch in text:
        if ch.isspace():
            buffer_is_ascii = False
            continue
        if ord(ch) < 128:
            if not buffer_is_ascii:
                tokens += 1
                buffer_is_ascii = True
        else:
            tokens += 1
            buffer_is_ascii = False
    return max(1, tokens)


VOCABS = {
    "char": vocab_char_heavy,
    "word": vocab_word_friendly,
}


class Handler(BaseHTTPRequestHandler):
    vocab = vocab_char_heavy
    fingerprint = "fp_" + "a" * 32
    model_name = "mock-model"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [{"id": self.model_name, "object": "model"}],
                },
            )
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}

        if not self.path.endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return

        messages = payload.get("messages") or []
        text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                text = message.get("content") or ""
                break

        prompt_tokens = self.vocab(text) + 8  # 固定请求开销
        # 数字分布指纹要几百个整数；给不同模型不同的「采样癖好」，
        # 这样分布通道才有区分力可验证。
        if "1 到 355" in text:
            answer = _integer_answer(self.model_name)
        else:
            # 单 token 探针（颜色/硬币/字母/随机数）——系统提示词是英文，
            # 所以这里先按特征词判断，命不中再退回通用回答
            answer = _one_word_answer(text, self.model_name) or _answer_for(text)

        self._send(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": self.model_name,
                "system_fingerprint": self.fingerprint,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": answer},
                     "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": max(1, len(answer) // 3),
                    "total_tokens": prompt_tokens + max(1, len(answer) // 3),
                },
            },
        )


# 各模型的采样 RNG。同一个模型每次调用推进同一条序列，所以分布稳定；
# 不同模型种子不同，分布形状也不同 —— onetoken 通道才有区分力可测。
_RNG_BY_MODEL: dict[str, Any] = {}


def _model_rng(model_name: str):
    if model_name not in _RNG_BY_MODEL:
        import random as _random

        _RNG_BY_MODEL[model_name] = _random.Random(sum(ord(c) for c in model_name) + 7)
    return _RNG_BY_MODEL[model_name]


# 故意做成偏斜分布（真实模型的答案分布也远非均匀，cell 熵中位数约 1.0 bit）
_COLOR_POOL = ["红", "蓝", "绿", "黄", "紫", "橙", "青", "白"]
_COIN_POOL = ["正面", "正面", "正面", "反面"]
_LETTER_POOL = list("ABCDEFGH")


def _pick(rng, pool: list[str], model_name: str) -> str:
    """按模型固定的偏斜采样：不同模型偏向池中不同位置。

    真实模型各有各的偏好中心，如果所有 mock 都均匀采样，
    JSD 就测不出差异，通道的区分力也就验证不了。
    """
    spread = max(1, len(pool) // 2)
    offset = sum(ord(c) for c in model_name) % spread
    index = int(abs(rng.gauss(0, 1.1)) + offset)
    return pool[min(len(pool) - 1, max(0, index))]


def _one_word_answer(text: str, model_name: str) -> str | None:
    """单 token 类问题：返回按模型固定的偏斜采样。"""
    rng = _model_rng(model_name)
    lowered = text.lower()
    if "颜色" in text or "color" in lowered:
        return _pick(rng, _COLOR_POOL, model_name)
    if "正面" in text or "heads" in lowered:
        return _pick(rng, _COIN_POOL, model_name)
    if "字母" in text or "letter" in lowered:
        return _pick(rng, _LETTER_POOL, model_name)
    if "随机数" in text or "random number" in lowered:
        center = 20 + (sum(ord(c) for c in model_name) % 3) * 30
        return str(max(1, min(100, int(rng.gauss(center, 18)))))
    return None


def _integer_answer(model_name: str) -> str:
    """生成一条整数序列回答。不同模型用不同的分布形状。

    alpha：均匀；beta：偏向小数字（前 40% 的区间占 70% 的样本）。
    两者边际分布差异明显，Hellinger 距离足以分开。
    """
    import random

    # 不能用 hash()：字符串的 hash 每次进程都变，测试就不稳定了
    rng = random.Random(sum(ord(c) for c in model_name))
    length = 300
    if model_name.endswith("beta"):
        pool = [rng.randint(1, 142) for _ in range(length * 7 // 10)]
        pool += [rng.randint(143, 355) for _ in range(length - len(pool))]
    else:
        pool = [rng.randint(1, 355) for _ in range(length)]
    rng.shuffle(pool)
    return ", ".join(str(v) for v in pool)


def _answer_for(text: str) -> str:
    """让 behavior 探针能得到可解析的回答。"""
    lowered = text.lower()
    if "0.1 + 0.2" in lowered:
        return "0.3"
    if "strawberry" in lowered:
        return "3"
    if "汉诺塔" in lowered or "hanoi" in lowered:
        return "31"
    if "知识截止" in lowered or "cutoff" in lowered:
        return "2026-04"
    if "上下文" in lowered or "context window" in lowered:
        return "200000"
    if "介绍你自己" in lowered:
        return "我是 mock-model，由 Mock 实验室开发，版本 1.0。"
    if lowered.strip() == "你好":
        return "你好！有什么可以帮你的吗？"
    if "反转字符串" in lowered or "reverse" in lowered:
        return "```python\ndef reverse_string(s):\n    return s[::-1]\n```"
    if "json" in lowered and "vendor" in lowered:
        return json.dumps(
            {
                "name": "mock-model",
                "vendor": "mock",
                "version": "1.0",
                "context_window": 200000,
                "knowledge_cutoff": "2026-04",
            },
            ensure_ascii=False,
        )
    if "收益率" in lowered:
        return "我无法保证收益，投资有风险。"
    if "translate" in lowered:
        return "该模型上个月更新过。"
    return "这是模拟回答。"


def serve(vocab_name: str, fingerprint: str, model_name: str, port: int):
    handler = type(
        f"Handler{port}",
        (Handler,),
        {
            "vocab": staticmethod(VOCABS[vocab_name]),
            "fingerprint": fingerprint,
            "model_name": model_name,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> int:
    print("══ ModelProof 端到端验证 ══\n")

    # 起两个端点：不同词表、不同后端指纹
    server_a = serve("char", "fp_" + "a" * 32, "mock-alpha", 7901)
    server_b = serve("word", "fp_" + "b" * 32, "mock-beta", 7902)
    print("  mock 端点：")
    print("    A  127.0.0.1:7901  词表=char  模型=mock-alpha")
    print("    B  127.0.0.1:7902  词表=word  模型=mock-beta\n")

    store = BaselineStore(ROOT / "tests" / "tmp_baselines")
    # 清掉上一次的残留
    for path in (ROOT / "tests" / "tmp_baselines").glob("*.json"):
        path.unlink()
    store.load(force=True)

    # -- 第一步：采集基准 ---------------------------------------------------
    print("【1】采集基准（模拟：我知道这两个端点的真实身份）")
    from modelproof.adapters import build_adapter
    from modelproof.core.baseline.schema import BaselineEntry, Identity, Provenance
    from modelproof.core.channels.behavior import (
        PARSERS,
        PROBES as B_PROBES,
        PROBE_VERSION as BH_VERSION,
    )
    from modelproof.core.channels.tokenizer import PROBES, PROBE_VERSION

    # 两个条目的知识截止故意设成不同，好让真伪模式有东西可比
    cutoff_by_model = {"mock-alpha": "2025-01", "mock-beta": "2026-04"}

    for port, model_id, vendor, family in (
        (7901, "mock-alpha", "mockvendor", "alpha"),
        (7902, "mock-beta", "mockvendor", "beta"),
    ):
        adapter = build_adapter(f"http://127.0.0.1:{port}/v1", "k", model_id)
        entry = BaselineEntry(
            model_id=model_id,
            identity=Identity(
                vendor=vendor,
                family=family,
                series="std",
                version="1.0",
                knowledge_cutoff=cutoff_by_model[model_id],
            ),
            provenance=Provenance(
                tier="official", method="e2e mock 自采", endpoint=f"127.0.0.1:{port}"
            ),
        )
        overhead = adapter.count_tokens("a")
        counts = {}
        for probe in PROBES:
            raw = adapter.count_tokens(probe["text"])
            counts[probe["id"]] = None if raw is None else max(0, raw - overhead)
        entry = store.upsert_from_observation(
            entry,
            "tokenizer",
            {"probe_version": PROBE_VERSION, "overhead": overhead, "counts": counts},
        )
        vector = {}
        for probe in B_PROBES:
            completion = adapter.complete(probe["prompt"], max_tokens=200)
            parser = PARSERS.get(probe["parser"])
            vector[probe["id"]] = (
                parser(completion.text) if (completion.ok and parser) else None
            )
        entry = store.upsert_from_observation(
            entry, "behavior", {"probe_version": BH_VERSION, "vector": vector}
        )
        store.save(entry)
        print(f"    ✓ {model_id}（37 格词表 + 12 项行为）")

    # -- 第二步：验证 -------------------------------------------------------
    print("\n【2】闭集判定")
    store.load(force=True)

    for port, expected, label in (
        (7901, "alpha", "A（应为 alpha）"),
        (7902, "beta", "B（应为 beta）"),
    ):
        report = run_verification(
            f"http://127.0.0.1:{port}/v1",
            "k",
            f"mock-{'alpha' if port == 7901 else 'beta'}",
            suite="standard",
            target_layer="family",
            store=store,
        )
        family = report.layers.get("family")
        got = family.value if family else None
        mark = "✓" if got == expected else "✗"
        print(
            f"    {mark} {label}：判定 {got}（置信度 "
            f"{family.confidence if family else 0:.0%}）"
        )
        if report.gateway:
            print(f"        链路：{report.gateway.get('label')}")
        if report.warnings:
            for warning in report.warnings:
                print(f"        ! {warning}")

    # -- 第三步：指纹变更检测 -----------------------------------------------
    print("\n【3】后端指纹台账与变更检测")
    from modelproof.core.channels.backend_fp import load_ledger

    ledger = load_ledger()
    for key, value in ledger.items():
        print(f"    {key} → {value.get('current')}")

    # 把端点 A 的指纹换掉，再跑一次，应该报 changed
    # 注意：shutdown() 只停掉循环，socket 还绑着，必须 server_close() 才能真正释放端口
    server_a.shutdown()
    server_a.server_close()
    server_a2 = serve("char", "fp_" + "c" * 32, "mock-alpha", 7901)
    report = run_verification(
        "http://127.0.0.1:7901/v1", "k", "mock-alpha",
        suite="quick", target_layer="family", store=store,
    )
    changed = any(
        e.channel == "backend_fp" and e.metric.get("changed")
        for e in report.evidence
    )
    print(f"    {'✓' if changed else '✗'} 指纹变更后检测到变化：{changed}")

    # -- 第四步：真伪模式 ---------------------------------------------------
    print("\n【4】真伪模式（故意报错身份）")
    report = run_verification(
        "http://127.0.0.1:7902/v1", "k", "mock-beta",
        suite="truth", claim="mock-alpha", target_layer="family", store=store,
    )
    truth = report.truthfulness
    print(f"    声明 mock-alpha，实际 mock-beta → {truth.verdict if truth else 'N/A'}")
    print(f"    退出码 {report.exit_code}（3 = 真伪不符）")

    for server in (server_a2, server_b):
        server.shutdown()
        server.server_close()

    print("\n══ 完成 ══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
