# ModelProof

**Verify what is actually behind an LLM endpoint.** Active fingerprinting,
layered verdicts, honest abstention.

A reseller says it serves `claude-opus-5-5`. Nothing in the API response
proves it. ModelProof sends a set of semantics-free probes, compares the
observations against measured fingerprints, and answers three questions:

1. **Which model is this?** — vendor → family → series → version, each layer
   with its own confidence
2. **Is it the model it claims to be?** — field-by-field cross-check
3. **Who built this entry point?** — official direct / official cloud gateway /
   subscription reverse-proxy / third-party relay

CLI, Web UI, and REST API share one core and emit the same
`VerificationReport` JSON.

## Why it exists

Silent model swaps are a real market problem: a provider routes your traffic
to a cheaper model, or quietly upgrades a model behind the same name and
breaks your prompts. Benchmarks tell you whether a model is *capable*; this
tool tells you whether the endpoint is *consistent with the model you picked*.

## Status

v0.2. The core, CLI, Web UI and REST API all run and pass end-to-end tests.
The bundled reference bank holds **127 entries, 60 of them with measured
fingerprints, across 16 vendors** (Claude ×12, GPT ×9, DeepSeek ×7,
Gemini ×7, Qwen ×4, GLM ×4, Grok ×3, Kimi ×2, MiniMax ×4, Hunyuan, MiMo,
Muse, Seed, Step, Meta), plus **12 measured tokenizer references**.

> **Abstention is a valid result.** When the 37-cell tokenizer probe hits
> several different vocabularies, the report says "requests are dispatched
> across multiple backends — no single identity exists" instead of forcing a
> guess. That kind of abstention *is* the answer.

## Install

```bash
pip install -e .
```

Requires Python ≥ 3.11 and only three runtime dependencies:
`flask`, `click`, `numpy`. The core layer is standard-library only.

## Quick start

```bash
# 1) See what it will cost — sends no requests
modelproof estimate --suite standard --model claude-opus-5-5

# 2) Collect a baseline from an endpoint you are authorized to test.
#    This is the precondition for any verdict: fingerprints need a reference.
modelproof baseline bank-create my-company --name "Internal endpoints"
modelproof baseline import https://api.anthropic.com -m claude-opus-5-5 \
  --vendor anthropic --family claude --series opus --tier official \
  --bank my-company --channels tokenizer,behavior,onetoken,backend_fp \
  --api-key $KEY

# 3) Verify an endpoint
modelproof verify https://api.example.com/v1 -m claude-opus-5-5 \
  --api-key $KEY --suite standard --bank my-company
```

Exit codes: `0` matched · `1` run failed · `2` inconclusive · `3` claim
mismatched (useful as a CI gate).

### Web UI

```bash
python -m modelproof.web.main 7861     # http://127.0.0.1:7861
```

The result page leads with four metric cards — most likely model, attribution
probability, identified family, valid samples — followed by a candidate
ranking table with probability bars. Everything else folds into an expandable
details section, because the first screen should answer "does it match?" and
nothing else.

Testing modes:

- **Automatic · quick** — pick a count (1–11), the tool chooses which items by
  channel priority (hard signals and zero-cost channels first)
- **Automatic · by category** — pick test categories, run every item under them
- **Manual** — tick individual test items

### REST API

```
POST   /v1/verifications              # start (async, returns task_id)
GET    /v1/verifications/{id}         # poll (progress has phase/item/percent)
GET    /v1/verifications/{id}/report  # report (json|md|html)
POST   /v1/estimates                  # cost estimate
GET    /v1/catalog                    # limits, defaults, categories, items
GET    /v1/history                    # past runs
```

## Channels

Twelve independent channels, grouped by how much they resist distillation:

**Hard signals** — fixed at training time, a wrapper cannot change them:

| Channel | What it measures |
|---|---|
| C1 tokenizer | 37-cell boundary-cost vector; a hit locks the vocabulary, hence the family |
| C2 distribution | marginal distribution of "random" integers in 1–355 |
| C2b robust pack | nuisance-projected class centroids + CV-calibrated beta + out-of-scope guard |
| C6 backend_fp | `system_fingerprint` (backend config hash), with a change ledger |

**Soft signals** — informative, but rewritable by a system prompt:

| Channel | What it measures |
|---|---|
| C3 behavior | 12 probes: self-reported identity, cutoff, context window, style |
| C4 style | layout density |
| C5 gateway | link axis vs behaviour axis, kept separate on purpose |
| C7 capability | capability cliff, auto-scored |
| C9 trace | TTFT / chunk interval / TPS |
| C10 validity | paraphrase / repetition / hallucination / canary |
| C11 onetoken | one-token preference distribution (arXiv 2607.10252) |
| C12 consistency | parameter / sampling / difficulty / fact consistency — not identity-scored |

The verdict is three-state: `MATCH` / `MISMATCH` / `INCONCLUSIVE`.
Closed-set softmax probabilities always normalise to 1, so the report never
presents a "best match" percentage as proof — it ships the gap ratio and an
out-of-scope guard alongside it, and abstains when the evidence does not carry.

## Design principles

- **Abstention over guessing.** A closed set must assign *some* candidate to an
  out-of-library model. The out-of-scope guard and `gap_ratio` exist so the
  report can say "not in the library" instead of naming the nearest neighbour.
- **Probe harmlessly.** Every probe is ordinary conversation (self-report,
  knowledge cutoff, letter counting, code style, over-promising on financial
  and legal advice). Nothing asks a model to produce harmful content.
- **Layered attribution.** A channel may only assert the layer it declares. If
  the threshold is not met, it abstains rather than rounding up.
- **Single signals never convict.** "There is a proxy in the middle" is not
  malice — your own company gateway produces the same signature.

See `RESPONSIBLE_USE.md` — read it before running this against anything.

## Reference data

| Source | What | License |
|---|---|---|
| `xqy2006/ModelTrace` | 18 GPT / Claude models with distribution fingerprints + robust pack + 6 tokenizer references | MIT |
| `Ikaleio/lm-detector` | 60 models / 13 families, dual-bank consistency check passed 18/18 before merge | MIT |
| HuggingFace / tiktoken | 12 tokenizer references built locally by `scripts/build_tokenizer_references.py` | upstream |

Both external banks are archived under `baselines/imports/` so that
`scripts/import_modeltrace.py` and `scripts/import_lmdetector.py` stay
reproducible without depending on sibling repositories.

## Project layout

```
modelproof/
  core/            # core layer — standard library only
    channels/      # the twelve probe channels
    suite/         # test-item registry (items, suites, categories, priority)
    baseline/      # banks, entries, provenance
    fusion/        # evidence fusion, robust pack, thresholds
  adapters/        # OpenAI / Anthropic protocol adapters
  cli.py           # verify / estimate / baseline / suite / report
  web/             # Flask app + Cloudscape-style UI (zero build)
baselines/         # reference banks + archived imports
scripts/           # import & build scripts
tests/             # e2e_channels / e2e_web / e2e_tasks / e2e_mock
DESIGN.md          # design doc with appendices A–J
```

## Tests

```bash
python tests/e2e_channels.py     # channels, multi-backend dispatch, budget ladder
python tests/e2e_web.py          # REST + pages
python tests/e2e_tasks.py        # task persistence + history
python tests/e2e_mock.py         # mock endpoints (verdicts, fingerprints, dispatch)
```

All four pass. Note that `e2e_web` may report a spurious HTTP 404 when run
right after `e2e_mock` (mock server port overlap) — re-run it alone to confirm.

## Known limits

- The robust pack covers **18 classes** (GPT ×8, Claude ×10). Fingerprints for
  the other 42 models carry marginal distributions only, so they take part in
  Hellinger comparison but not in the robust classifier. Adding a class needs
  retraining on raw responses.
- **Anthropic does not publish its tokenizer**, so there is no local Claude
  vocabulary reference. C1 structurally abstains on every Claude model — a
  known gap, not a defect of the endpoint under test.
- C11 distance thresholds are still the paper's placeholders; recalibrate on
  real endpoints with `baseline recalibrate`.
- Open-set recognition reports **bands, not probabilities**. Without labelled
  out-of-library samples, any "73% chance of being out of library" would be
  invented.
- Statistical fingerprints **cannot prove training lineage**. Similarity may
  be distillation, coincidence, or genuinely serving that model.

---

[中文说明](README.zh-CN.md) · [Responsible use](RESPONSIBLE_USE.md) ·
[Design](DESIGN.md) · Related: [`ModelTrace`](https://github.com/xqy2006/ModelTrace)
