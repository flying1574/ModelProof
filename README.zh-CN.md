# ModelProof

**验证 LLM 端点背后到底跑的是什么模型。** 主动指纹探测 · 分层判定 · 诚实弃权。

中转商声称提供 `claude-opus-5-5`，API 响应里没有任何东西能证明这件事。
ModelProof 发一组与语义无关的探针，把观测结果与实测指纹比对，回答三个问题：

1. **这是什么模型？** —— 厂商 → 家族 → 系列 → 版本，逐层给置信度
2. **它是不是自己声称的那个？** —— 逐字段交叉核验
3. **这个入口是谁搭的？** —— 官方直连 / 官方云网关 / 订阅反代 / 第三方中转

CLI、Web 界面、REST API 共用同一套内核，产出同一个 `VerificationReport` JSON。

## 为什么需要它

静默换模型是真实存在的市场问题：供应商把你的流量路由到更便宜的模型，
或者在同一个模型名下悄悄升级、打乱你的 prompt。基准测试回答「这个模型
行不行」，这个工具回答「这个端点和你选的模型一致不一致」。

## 当前状态

v0.2。内核、CLI、Web、REST API 均已可运行并通过端到端测试。
内置基准库 **127 条条目，其中 60 条带实测指纹，覆盖 16 个厂商**
（Claude ×12、GPT ×9、DeepSeek ×7、Gemini ×7、Qwen ×4、GLM ×4、
Grok ×3、Kimi ×2、MiniMax ×4、Hunyuan、MiMo、Muse、Seed、Step、Meta），
另有 **12 份实测词表参考**。

> **弃权是有效结论。** 当 37 格词表探针命中多个不同词表时，报告会写
> 「请求被分派到多个后端，不存在单一身份」，而不是硬塞一个最像的候选。
> 这种弃权本身就是答案。

## 安装

```bash
pip install -e .
```

需要 Python ≥ 3.11，运行时仅三个依赖：`flask`、`click`、`numpy`。
core 层只用标准库。

## 快速开始

```bash
# 1) 先看要花多少（不发任何请求）
modelproof estimate --suite standard --model claude-opus-5-5

# 2) 从你有权访问的端点采集基准 —— 这是判定能工作的前提：指纹需要有参照
modelproof baseline bank-create my-company --name "公司内部端点"
modelproof baseline import https://api.anthropic.com -m claude-opus-5-5 \
  --vendor anthropic --family claude --series opus --tier official \
  --bank my-company --channels tokenizer,behavior,onetoken,backend_fp \
  --api-key $KEY

# 3) 验证一个端点
modelproof verify https://api.example.com/v1 -m claude-opus-5-5 \
  --api-key $KEY --suite standard --bank my-company
```

退出码：`0` 判定一致 · `1` 运行失败 · `2` 弃权 · `3` 真伪不符（可在 CI 里当阻断）。

### Web 界面

```bash
python -m modelproof.web.main 7861     # http://127.0.0.1:7861
```

结果页第一屏是四个指标卡——最可能模型、归因概率、识别家族、有效查询，
下面跟带概率条的候选排名表。其余全部收进可展开的详情区，因为第一屏只该
回答「像不像」。

测试模式：

- **自动 · 快速** —— 填数量（1–11），取哪几项由工具按通道优先级决定
  （硬信号与零成本通道排在前）
- **自动 · 特定项目** —— 勾选测试大类，跑该类下全部项
- **手动** —— 逐项勾选具体测试项

### REST API

```
POST   /v1/verifications              # 发起验证（async，返回 task_id）
GET    /v1/verifications/{id}         # 轮询（progress 含阶段/当前项/百分比）
GET    /v1/verifications/{id}/report  # 报告（json|md|html）
POST   /v1/estimates                  # 消耗预估
GET    /v1/catalog                    # 上下限、默认值、大类与明细
GET    /v1/history                    # 历史列表
```

## 通道

十二条独立通道，按抗蒸馏能力分组：

**硬信号** —— 训练时即固定，套壳改不了：

| 通道 | 测什么 |
|---|---|
| C1 tokenizer | 37 格边界代价向量；命中即锁定词表，进而锁定家族 |
| C2 distribution | 「随机」整数（1–355）的边际分布 |
| C2b 稳健打分包 | 干扰投影后的类心 + 交叉验证标定 beta + 库外守卫 |
| C6 backend_fp | `system_fingerprint`（后端配置哈希）+ 变更台账 |

**软信号** —— 有信息量，但可被 system prompt 改写：

| 通道 | 测什么 |
|---|---|
| C3 behavior | 12 条探针：自述身份、知识截止、上下文窗口、代码风格 |
| C4 style | 排版密度 |
| C5 gateway | 链路轴与行为轴分开判，不混为一谈 |
| C7 capability | 能力悬崖，自动判分 |
| C9 trace | TTFT / chunk 间隔 / TPS |
| C10 validity | 释义 / 重复 / 幻觉 / canary |
| C11 onetoken | 单 token 偏好分布（arXiv 2607.10252） |
| C12 consistency | 参数 / 采样 / 难度 / 事实一致性，不参与身份打分 |

裁决是三态：`MATCH` / `MISMATCH` / `INCONCLUSIVE`。
闭集 softmax 概率必然归一到 1，所以报告从不把「最像」的百分比当作证据——
它同时给出 `gap_ratio` 与库外守卫，证据不够就弃权。

## 设计原则

- **宁弃权，不硬猜。** 闭集归因在数学上必然给库外模型指派某个候选。
  库外守卫与 `gap_ratio` 就是为了让报告能说「不在库内」，而不是点名最近邻。
- **探针必须无害。** 全部探针都是常规问答（自述、知识截止、字母计数、
  代码风格、金融法律过度承诺），绝不要求模型输出有害内容。
- **分层归因。** 通道只能证明到它声明的那一层，达不到阈值就弃权，不向上取整。
- **单条信号永远不定罪。** 「中间有一层代理」不等于恶意——你自己公司的
  网关也会产生同样特征。

使用前请读 `RESPONSIBLE_USE.md`。

## 参考数据

| 来源 | 内容 | 许可 |
|---|---|---|
| `xqy2006/ModelTrace` | 18 个 GPT / Claude 模型的分布指纹 + 稳健打分包 + 6 份词表参考 | MIT |
| `Ikaleio/lm-detector` | 60 个模型 / 13 个家族，合并前通过 18/18 双库一致性校验 | MIT |
| HuggingFace / tiktoken | 12 份词表参考，由 `scripts/build_tokenizer_references.py` 本地构建 | 上游许可 |

两份外部数据都归档在 `baselines/imports/`，使 `import_modeltrace.py` 与
`import_lmdetector.py` 的复现不依赖兄弟仓库。

## 目录结构

```
modelproof/
  core/            # 内核 —— 只用标准库
    channels/      # 十二条探针通道
    suite/         # 测试项注册表（项、套件、大类、优先级）
    baseline/      # 基准库、条目、溯源
    fusion/        # 证据融合、稳健打分包、阈值
  adapters/        # OpenAI / Anthropic 协议适配器
  cli.py           # verify / estimate / baseline / suite / report
  web/             # Flask 应用 + Cloudscape 风格界面（零构建）
baselines/         # 参考库 + 归档的外部数据
scripts/           # 导入与构建脚本
tests/             # e2e_channels / e2e_web / e2e_tasks / e2e_mock
DESIGN.md          # 设计文档，附录 A–J
```

## 测试

```bash
python tests/e2e_channels.py     # 通道、多后端分派、预算阶梯
python tests/e2e_web.py          # REST + 页面
python tests/e2e_tasks.py        # 任务持久化 + 历史
python tests/e2e_mock.py         # mock 端点（裁决、指纹、分派）
```

四个全绿。注意 `e2e_web` 紧跟 `e2e_mock` 跑时可能报一次假的 HTTP 404
（mock server 端口重叠），单独重跑即可确认。

## 已知边界

- 稳健打分包只覆盖 **18 类**（GPT ×8、Claude ×10）。另外 42 条指纹只有
  边际分布，能参与 Hellinger 比对但不进稳健分类器。加新类需要用原始
  回答重新训练。
- **Anthropic 不公开词表**，本地没有 Claude 的词表参考可建。C1 对任何
  Claude 模型结构性弃权——这是已知缺口，不是被测端点的问题。
- C11 的距离阈值仍是照论文设的占位值，需在真实端点上用
  `baseline recalibrate` 标定。
- 开集识别给的是**档位，不是概率**。没有带标注的库外样本，任何
  「73% 概率在库外」都是编的。
- 统计指纹**无法证明训练血缘**。相似可能是蒸馏、可能是巧合、也可能
  真的在服务那个模型。

---

[English](README.md) · [负责任使用](RESPONSIBLE_USE.md) · [设计文档](DESIGN.md) ·
相关项目：[`ModelTrace`](https://github.com/xqy2006/ModelTrace)
