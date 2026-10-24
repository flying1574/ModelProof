# ModelTrace 归档副本

这里是 **ModelTrace 数据的一份冻结副本**，`scripts/import_modeltrace.py`
默认就读这里，不再依赖 `D:/data/working/ModelTrace` 那个兄弟仓库。

## 为什么要收一份进来

ModelTrace 是独立仓库，会被 reset、被拉新、被重构。10-21 真的断过一次：
ModelTrace 被 `git reset --hard` + `git clean` 回上游纯净状态，而它本地
那批 `data/*.json` / `data/*.jsonl` **从来没提交过上游**，一清就全没了，
`import_modeltrace.py` 当场 `FileNotFoundError`。

数据本身早已导入 ModelProof（库里 18 条带指纹的条目），受影响的只是
「重新导入」这条复现路径。把副本收进来之后，复现只取决于 ModelProof 自己。

## 收了哪些

| 文件 | 大小 | 用途 |
|---|---|---|
| `data/unified_bank.json` | 965 KB | 18 模型的 355 维计数 + robust 包 + calibration |
| `data/tokenizer_references.json` | 7.4 KB | 6 份词表参考（37 格，tk1-37） |
| `data/gpt_reference.jsonl` | 2.4 MB | GPT 的原始回答，用来算 74 维**有序块**特征 |
| `data/claude_reference.jsonl` | 3.4 MB | 同上，Claude |
| `data/claude-openrouter_reference.jsonl` | 607 KB | claude-opus-5.5 / sonnet-5.5 的额外采样 |

三个 jsonl 不能省：`unified_bank.json` 只有汇总计数，**没有数字顺序**，
重建不出 `ordered_block_feature`。没有它们，导入的 18 条就只剩边际分布。

## 版本与边界

- 这是 ModelTrace **2026-10-13 重建版**（`built_at` 见文件内），
  不是上游 HEAD 那份 10-09 版。差别在 `claude-opus-5.5`（108 条回答，
  合并了 claude-openrouter 的 3 倍采样）和 `claude-sonnet-5.5`（72 条）。
  库里现有 18 条就是从这个版本导入的，所以重跑是幂等的。
- 覆盖只有 **GPT 与 Claude 两个家族**。
- 采集时间 2026-08 ~ 2026-10，模型可能已换版本，溯源里带了 `as_of`。
- 上游仓库 `xqy2006/ModelTrace` 是 MIT；`lm.ikale.io` 的社区库也致谢了它。
  这份副本按 MIT 兼容使用，仅用于本项目复现。

## 用法

```bash
# 默认就读这里
python scripts/import_modeltrace.py --dry-run

# 想用别处的副本（比如重新 clone 的上游）
python scripts/import_modeltrace.py --source /path/to/ModelTrace
```

`find_source()` 的顺序是：显式 `--source` > 本归档目录 > 外部 ModelTrace 仓库。
