"""生成基准库骨架条目。

重要：这里生成的条目**只有身份元数据，没有实测指纹**。指纹必须由
`modelproof baseline import` 从你有权访问的真实端点采集。

之所以仍然生成这些骨架：
  1. 让基准库一开始就有完整的候选集，真伪模式能立刻比对元数据；
  2. 明确标出「哪些模型还缺指纹」，采集进度一目了然。

溯源一律标 derived —— 推导数据不能单独支撑结论，这条纪律在这里就生效。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modelproof.core.baseline.schema import BaselineEntry, Identity, Provenance  # noqa: E402
from modelproof.core.baseline.store import BaselineStore  # noqa: E402

AS_OF = "2026-10-17"

# (model_id, vendor, family, series, version, knowledge_cutoff)
MODELS: list[tuple[str, str, str, str, str, str]] = [
    # -- Anthropic --------------------------------------------------------
    ("claude-fable-5.1", "anthropic", "claude", "fable", "5.1", ""),
    ("claude-fable-5", "anthropic", "claude", "fable", "5", ""),
    ("claude-opus-5.5", "anthropic", "claude", "opus", "5.5", ""),
    ("claude-opus-5", "anthropic", "claude", "opus", "5", ""),
    ("claude-opus-4.8", "anthropic", "claude", "opus", "4.8", ""),
    ("claude-opus-4.7", "anthropic", "claude", "opus", "4.7", ""),
    ("claude-sonnet-5.5", "anthropic", "claude", "sonnet", "5.5", ""),
    ("claude-sonnet-5", "anthropic", "claude", "sonnet", "5", ""),
    ("claude-sonnet-4.6", "anthropic", "claude", "sonnet", "4.6", ""),
    ("claude-haiku-5.5", "anthropic", "claude", "haiku", "5.5", ""),
    ("claude-haiku-4.5", "anthropic", "claude", "haiku", "4.5", ""),
    # -- OpenAI -----------------------------------------------------------
    ("gpt-6-astra", "openai", "gpt", "astra", "6", "2026-04-30"),
    ("gpt-6.1-sol", "openai", "gpt", "sol", "6.1", "2026-04-30"),
    ("gpt-6-sol", "openai", "gpt", "sol", "6", "2026-04-30"),
    ("gpt-6-luna", "openai", "gpt", "luna", "6", "2026-05-18"),
    ("gpt-5.6-sol", "openai", "gpt", "sol", "5.6", "2026-02-16"),
    ("gpt-5.6-terra", "openai", "gpt", "terra", "5.6", "2026-02-16"),
    ("gpt-5.6-luna", "openai", "gpt", "luna", "5.6", "2026-02-16"),
    ("gpt-5.6-cyber", "openai", "gpt", "cyber", "5.6", "2026-02-16"),
    ("gpt-5.5", "openai", "gpt", "standard", "5.5", ""),
    ("gpt-5.4", "openai", "gpt", "standard", "5.4", ""),
    # -- Google -----------------------------------------------------------
    ("gemini-4-argon", "google", "gemini", "pro", "4", ""),
    ("gemini-3.8-flash", "google", "gemini", "flash", "3.8", ""),
    ("gemini-3.7-flash", "google", "gemini", "flash", "3.7", ""),
    ("gemini-3.6-flash", "google", "gemini", "flash", "3.6", ""),
    ("gemini-3.5-flash", "google", "gemini", "flash", "3.5", ""),
    ("gemini-3.5-flash-lite", "google", "gemini", "flash-lite", "3.5", ""),
    ("gemini-3.1-pro-preview", "google", "gemini", "pro", "3.1", ""),
    ("gemma-4-31b-it", "google", "gemma", "it", "4-31b", ""),
    ("gemma-4-26b-a4b-it", "google", "gemma", "it", "4-26b", ""),
    # -- Alibaba ----------------------------------------------------------
    ("qwen3.6-plus", "alibaba", "qwen", "plus", "3.6", ""),
    ("qwen3.6-35b-a3b", "alibaba", "qwen", "a3b", "3.6-35b", ""),
    ("qwen3.6-27b", "alibaba", "qwen", "dense", "3.6-27b", ""),
    ("qwen3.5-397b-a17b", "alibaba", "qwen", "a17b", "3.5-397b", ""),
    ("qwen3.5-plus", "alibaba", "qwen", "plus", "3.5", ""),
    ("qwen3.5-max", "alibaba", "qwen", "max", "3.5", ""),
    ("qwen3.8-27b", "alibaba", "qwen", "dense", "3.8-27b", ""),
    ("qwen-vl-max", "alibaba", "qwen", "vl", "max", ""),
    # -- DeepSeek ---------------------------------------------------------
    ("deepseek-v4.1-flash-1017", "deepseek", "deepseek", "flash", "4.1", ""),
    ("deepseek-v4-pro", "deepseek", "deepseek", "pro", "4", ""),
    ("deepseek-v4-flash", "deepseek", "deepseek", "flash", "4", ""),
    ("deepseek-v3.2", "deepseek", "deepseek", "standard", "3.2", ""),
    ("deepseek-chat", "deepseek", "deepseek", "chat", "v4", ""),
    ("deepseek-reasoner", "deepseek", "deepseek", "reasoner", "v4", ""),
    ("deepseek-v4-lite", "deepseek", "deepseek", "lite", "4", ""),
    ("deepseek-coder-v3", "deepseek", "deepseek", "coder", "3", ""),
    # -- Moonshot ---------------------------------------------------------
    ("kimi-k3", "moonshot", "kimi", "k3", "3", ""),
    ("kimi-k2.6", "moonshot", "kimi", "k2", "2.6", ""),
    ("kimi-k2.5", "moonshot", "kimi", "k2", "2.5", ""),
    ("kimi-k2", "moonshot", "kimi", "k2", "2", ""),
    ("kimi-k1.5", "moonshot", "kimi", "k1", "1.5", ""),
    ("moonshot-v1-128k", "moonshot", "kimi", "v1", "128k", ""),
    ("kimi-vl-a3b", "moonshot", "kimi", "vl", "a3b", ""),
    ("kimi-dev-72b", "moonshot", "kimi", "dev", "72b", ""),
    # -- Zhipu ------------------------------------------------------------
    ("glm-5.3-flash", "zhipu", "glm", "flash", "5.3", ""),
    ("glm-5.2", "zhipu", "glm", "standard", "5.2", ""),
    ("glm-5.1", "zhipu", "glm", "standard", "5.1", ""),
    ("glm-5", "zhipu", "glm", "standard", "5", ""),
    ("glm-4.6", "zhipu", "glm", "standard", "4.6", ""),
    ("glm-4.5-air", "zhipu", "glm", "air", "4.5", ""),
    ("glm-4.5", "zhipu", "glm", "standard", "4.5", ""),
    ("glm-4-plus", "zhipu", "glm", "plus", "4", ""),
    # -- Meta -------------------------------------------------------------
    ("llama-4-behemoth", "meta", "llama", "behemoth", "4", ""),
    ("llama-4-maverick", "meta", "llama", "maverick", "4", ""),
    ("llama-4-scout", "meta", "llama", "scout", "4", ""),
    ("llama-4-mini", "meta", "llama", "mini", "4", ""),
    ("llama-3.3-70b", "meta", "llama", "dense", "3.3-70b", ""),
    ("llama-3.1-405b", "meta", "llama", "dense", "3.1-405b", ""),
    ("muse-glimmer-30b", "meta", "muse", "glimmer", "30b", ""),
    ("llama-guard-4", "meta", "llama", "guard", "4", ""),
    # -- Mistral ----------------------------------------------------------
    ("mistral-large-3", "mistral", "mistral", "large", "3", ""),
    ("mistral-medium-3.5", "mistral", "mistral", "medium", "3.5", ""),
    ("mistral-small-4", "mistral", "mistral", "small", "4", ""),
    ("codestral-2", "mistral", "mistral", "codestral", "2", ""),
    ("magistral-medium", "mistral", "mistral", "magistral", "1", ""),
    ("ministral-8b", "mistral", "mistral", "ministral", "8b", ""),
    ("mistral-embed-2", "mistral", "mistral", "embed", "2", ""),
    ("devstral-2", "mistral", "mistral", "devstral", "2", ""),
    # -- xAI --------------------------------------------------------------
    ("grok-4.7", "xai", "grok", "standard", "4.7", ""),
    ("grok-4.6", "xai", "grok", "standard", "4.6", ""),
    ("grok-4-fast", "xai", "grok", "fast", "4", ""),
    ("grok-4-mini", "xai", "grok", "mini", "4", ""),
    ("grok-3.5", "xai", "grok", "standard", "3.5", ""),
    ("grok-3-mini", "xai", "grok", "mini", "3", ""),
    ("grok-vision-4", "xai", "grok", "vision", "4", ""),
    ("grok-code-4", "xai", "grok", "code", "4", ""),
    # -- MiniMax ----------------------------------------------------------
    ("minimax-m3", "minimax", "minimax", "m", "3", ""),
    ("minimax-m2.7", "minimax", "minimax", "m", "2.7", ""),
    ("minimax-m2", "minimax", "minimax", "m", "2", ""),
    ("minimax-m1", "minimax", "minimax", "m", "1", ""),
    ("hailuo-3", "minimax", "minimax", "hailuo", "3", ""),
    ("minimax-text-01", "minimax", "minimax", "text", "01", ""),
    ("abab7", "minimax", "minimax", "abab", "7", ""),
    ("minimax-vl-01", "minimax", "minimax", "vl", "01", ""),
    # -- NVIDIA -----------------------------------------------------------
    ("nemotron-3-ultra", "nvidia", "nemotron", "ultra", "3", ""),
    ("nemotron-3-super", "nvidia", "nemotron", "super", "3", ""),
    ("nemotron-3-nano", "nvidia", "nemotron", "nano", "3", ""),
    ("nemotron-2-70b", "nvidia", "nemotron", "dense", "2-70b", ""),
    ("nemotron-4-340b", "nvidia", "nemotron", "dense", "4-340b", ""),
    ("llama-3.1-nemotron-70b", "nvidia", "nemotron", "dense", "3.1-70b", ""),
    ("nemotron-mini-4b", "nvidia", "nemotron", "mini", "4b", ""),
    ("nv-embedqa-2", "nvidia", "nemotron", "embed", "2", ""),
]

# 别名：同一模型的常见写法
ALIASES: dict[str, list[str]] = {
    "claude-opus-5.5": ["claude-opus-5-5", "opus-5.5"],
    "claude-sonnet-5.5": ["claude-sonnet-5-5", "sonnet-5.5"],
    "claude-haiku-5.5": ["claude-haiku-5-5", "haiku-5.5"],
    "claude-fable-5.1": ["claude-fable-5-1", "fable-5.1"],
    "gpt-6-astra": ["gpt-6-astra-2026-05"],
    "deepseek-chat": ["deepseek-v4-chat"],
}


def main() -> int:
    store = BaselineStore()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    created = 0
    for model_id, vendor, family, series, version, cutoff in MODELS:
        existing = store.get(model_id)
        if existing is not None and existing.channels:
            # 已有实测指纹，别覆盖
            continue
        entry = BaselineEntry(
            model_id=model_id,
            aliases=ALIASES.get(model_id, []),
            identity=Identity(
                vendor=vendor,
                family=family,
                series=series,
                version=version,
                knowledge_cutoff=cutoff,
            ),
            channels={},
            provenance=Provenance(
                tier="derived",
                method=f"整理自厂商公开文档（{AS_OF} 核对）",
                collected_at=now,
                note=(
                    "仅含身份元数据，**尚无实测指纹**。请用 "
                    "`modelproof baseline import` 采集 tokenizer / behavior 数据后，"
                    "本条目才能参与闭集判定。"
                ),
            ),
        )
        store.save(entry)
        created += 1

    stats = store.stats()
    print(f"✓ 写入 {created} 条骨架条目，基准库共 {stats['total']} 条")
    print(f"  按厂商：{stats['by_vendor']}")
    print(f"  按溯源：{stats['by_tier']}")
    print("")
    print("  注意：全部条目当前只有元数据，没有 tokenizer/behavior 指纹。")
    print("  采集后条目才会参与闭集判定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
