"""Web / REST 端到端验证。

起一个 mock 模型端点，再用 REST API 走完整流程：
  预估 → 发起验证（async）→ 轮询 → 取报告（json / md / html）
顺带检查三个页面能正常渲染。

用法：
    python tests/e2e_web.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.e2e_mock import serve  # noqa: E402

WEB_PORT = 7862
MOCK_PORT = 7903
BASE = f"http://127.0.0.1:{WEB_PORT}"


# 本机端点一律直连：环境里的 http_proxy 会随机拦住环回连接（表现为请求挂住），
# 那是测试基础设施的问题，不该算到被测代码头上。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, path: str, payload: dict | None = None, raw: bool = False):
    url = BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with _OPENER.open(request, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return (body if raw else json.loads(body)), resp.status


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _OPENER.open(f"http://127.0.0.1:{port}/v1/healthz", timeout=2).read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def _start_web(port: int) -> tuple[subprocess.Popen, list[str]]:
    """起 Web 服务，并**把它的输出抽干**。

    不抽干的话：子进程写满 stdout 管道缓冲区就会阻塞在日志上，表现为
    「TCP 连上了但服务端永远不发状态行」——看着像服务挂了，其实是测试
    自己把管道憋满的。Werkzeug 每请求打一行，跑满一轮就够填满。
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "modelproof.web.main", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    lines: list[str] = []

    def drain() -> None:
        if proc.stdout is None:
            return
        for raw in iter(proc.stdout.readline, b""):
            lines.append(raw.decode("utf-8", "replace"))
        proc.stdout.close()

    threading.Thread(target=drain, daemon=True).start()
    return proc, lines


def main() -> int:
    print("══ ModelProof Web 端到端验证 ══\n")

    mock = serve("char", "fp_" + "d" * 32, "mock-gamma", MOCK_PORT)
    print(f"  mock 端点 127.0.0.1:{MOCK_PORT}（模型 mock-gamma，词表 char）")

    proc, log = _start_web(WEB_PORT)
    try:
        if not wait_ready(WEB_PORT):
            print("✗ Web 服务未能启动")
            print("".join(log[-40:]))
            return 1
        print(f"  Web 服务 {BASE}\n")

        # -- 1. healthz ---------------------------------------------------
        data, _ = http("GET", "/v1/healthz")
        print(f"【1】healthz：{data['status']}，基准 {data['baselines']} 条，"
              f"通道 {len(data['channels'])} 个")

        # -- 2. 预估 -------------------------------------------------------
        data, _ = http("POST", "/v1/estimates", {
            "suite": "standard", "model": "mock-gamma", "price_table": "openai",
        })
        print("【2】预估：")
        print("    " + data["formatted"].replace("\n", "\n    "))

        # -- 3. 发起验证 ----------------------------------------------------
        created, status = http("POST", "/v1/verifications", {
            "base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
            "api_key": "test-key",
            "model": "mock-gamma",
            "suite": "quick",
            "target_layer": "family",
        })
        task_id = created["id"]
        print(f"【3】发起验证：{task_id}（HTTP {status}，{created['status']}）")

        # -- 4. 轮询 --------------------------------------------------------
        report = None
        for _ in range(200):
            task, _ = http("GET", f"/v1/verifications/{task_id}")
            if task["status"] != "running":
                report = task.get("report")
                print(f"【4】轮询完成：{task['status']}")
                break
            time.sleep(0.5)
        else:
            print("【4】✗ 轮询超时")
            return 1

        if not report:
            print("    ✗ 没有拿到报告")
            return 1

        print(f"    run_id {report['run_id']}")
        for layer, verdict in report["layers"].items():
            value = verdict["value"] or "弃权"
            print(f"    {layer}: {value}（{verdict['confidence']:.0%}）"
                  f"  {verdict.get('detail', '')[:50]}")
        print(f"    证据 {len(report['evidence'])} 条，链路 "
              f"{(report.get('gateway') or {}).get('label', '—')}")

        # -- 5. 报告三种格式 ------------------------------------------------
        md, _ = http("GET", f"/v1/verifications/{task_id}/report?format=md", raw=True)
        html, _ = http("GET", f"/v1/verifications/{task_id}/report?format=html", raw=True)
        print(f"【5】报告格式：md {len(md)} 字符 · html {len(html)} 字符")

        # -- 6. 页面 --------------------------------------------------------
        for path in ("/", "/baselines", f"/report/{task_id}"):
            with _OPENER.open(BASE + path, timeout=10) as resp:
                body = resp.read().decode("utf-8")
            ok = resp.status == 200 and len(body) > 500
            print(f"【6】页面 {path}: {'✓' if ok else '✗'} ({len(body)} 字符)")

        print("\n══ 完成 ══")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()
        mock.server_close()


if __name__ == "__main__":
    sys.exit(main())
