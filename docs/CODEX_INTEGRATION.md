# Codex 接入说明

把 Codex CLI 当作**并行编码工**使用：主 Agent（DSH）负责拆任务、写验收标准、审查 diff 和跑测试；Codex 负责单个自包含任务的实现。Codex 的产出**不自动合并**，一律经过人工/主 Agent 审查。

实现入口：[`scripts/codex_task.py`](../scripts/codex_task.py)

---

## 1. 环境前提

| 项 | 要求 |
|---|---|
| Codex CLI | `npm i -g @openai/codex`，已验证 0.155.1 |
| 登录 | `codex login`，`codex login status` 显示 `Logged in using ChatGPT` |
| Python | 3.9+，只用标准库 |

`scripts/codex_task.py` 会自动把 `CODEX_HOME` 指向仓库内的 `.codex-home/`，并软链 `~/.codex/auth.json` 复用登录态。原因见第 3 节。

---

## 2. 基本用法

```bash
# 单任务
python3 scripts/codex_task.py "为 utils/text.py 补单元测试，只新增 tests/test_text_utils.py"

# 长任务 + 指定模型
python3 scripts/codex_task.py --model gpt-5-codex --timeout 1800 --task-file /tmp/task.md

# 代码审查模式（只读，不许改文件）
python3 scripts/codex_task.py --mode review --uncommitted "重点看并发与异常处理"

# 只读任务，明确禁止写盘
python3 scripts/codex_task.py --sandbox read-only "解释 inference/tool_dag.py 的调度逻辑"
```

每次运行会在 `<repo>/.codex-runs/<时间戳>/` 下落盘：

| 文件 | 内容 |
|---|---|
| `transcript.jsonl` | Codex 原始事件流（含每条命令与退出码） |
| `last_message.md` | Codex 最终回复 |
| `report.md` | 任务、用量、执行命令数、改动文件、diff 统计 |
| `changes.patch` | 本次任务新产生的 `git diff`（无改动则不生成） |
| `plan.json` | plan 模式产出的结构化计划（其他模式不生成） |
| `stderr.log` | 标准错误 |

脚本最后向 stdout 打印 JSON 摘要，退出码：`0` 成功 / `1` Codex 失败 / `2` 超时 / `3` 环境或参数错误。

### 2.1 规划模式：Codex 只出计划，主 Agent 执行

```bash
python3 scripts/codex_task.py --mode plan --reasoning-effort xhigh "给评测聚合逻辑补测试，先别改代码"
```

- 默认 `--reasoning-effort xhigh`（可用 `minimal|low|medium|high|xhigh` 覆盖）；
- 强制 `--output-schema scripts/codex_plan_schema.json`，产出机器可读 JSON：
  `goal` / `assumptions[]` / `steps[{id, action, files[], acceptance}]` / `risks[]` / `out_of_scope[]`；
- 提示词要求「只读、不许改文件」，`changes.patch` 应为空；
- 摘要多出 `plan` 字段；计划解析失败时 `ok=false`、退出码 1，不会把一段散文当成计划。

分工约定：

1. **Codex 规划**：先读代码确认现状，再写出带文件清单与验收条件的步骤；
2. **主 Agent 执行**：逐步落地，每步跑验收命令；执行前先核对计划里的函数名/路径是否真实存在（规划阶段不跑测试，容易想当然）；
3. **Codex 复核（可选）**：`--mode review --uncommitted` 审落实情况，按 阻断/重要/次要 分级。

---

## 3. 两个必须知道的坑

### 3.1 嵌套沙箱不可用 → 内层改用 `danger-full-access`

Codex 在 macOS 上用自己的 seatbelt 沙箱执行命令。把它跑在 DSH 的沙箱里时，内层沙箱无法初始化，**所有命令都会失败**：

```text
Error: failed to initialize in-process app-server client: Operation not permitted (os error 1)
（能起会话时）沙箱拒绝执行 `ls -1`
```

因此 `--sandbox auto` 的规则是：

- 检测到 `DSH_SHELL=1`（跑在 DSH 沙箱内）→ 用 `danger-full-access`，由**外层 DSH 沙箱**兜底（实际可写范围仍只有工作区）；
- 没有外层沙箱 → 用 `workspace-write`；
- 想在任何情况下都用 `danger-full-access`，必须显式加 `--allow-unsandboxed`，否则脚本直接拒绝。

### 3.2 `~/.codex` 不可写 → 用仓库内 `CODEX_HOME`

DSH 沙箱只允许写工作区，Codex 启动时要写 `state_*.sqlite`，否则：

```text
failed to open state DB at /Users/sheep/.codex/state_5.sqlite: attempt to write a readonly database
```

脚本默认 `CODEX_HOME=<repo>/.codex-home`，并软链 `auth.json`。该目录已加入 `.gitignore`。

副作用：这个 `CODEX_HOME` 是干净的，不加载 `~/.codex/config.toml` 里的插件与 MCP，启动更快、行为更可预测；需要时可用环境变量 `CODEX_HOME` 覆盖。

---

## 4. 任务书写规范

Codex 是单轮、非交互的，任务描述必须自包含。派单时至少包含四项：

1. **目标**：要改什么、为什么改；
2. **边界**：允许改哪些文件（例如「只新增 `tests/test_text_utils.py`，不改源码」）；
3. **验收**：跑什么命令算通过（例如 `python3 -m pytest tests/test_text_utils.py -q`）；
4. **现状**：相关文件的路径与关键函数名，避免它全仓库乱翻。

反面例子：「优化一下代码」「修一下 bug」——没有边界和验收，产出不可审查。

---

## 5. 审查流程

1. 看 `report.md` 的改动文件列表，确认没有越界修改；
2. `git diff`（或 `changes.patch`）逐段读，重点看异常处理、边界值、是否偷偷改测试来迁就实现；
3. 由**主 Agent 自己**跑验收命令，不采信 Codex 的自述；
4. 通过则保留，不通过就让 Codex `exec resume` 续跑同一会话，或回滚后重派。

```bash
git checkout -- .            # 整体回滚
git clean -fd tests/         # 清掉新增的未跟踪文件
```

---

## 6. 适用与不适用

**适合**：补单元测试、单文件重构、写文档/注释、批量机械改写、按明确规格实现小模块、代码审查。

**不适合**：需要跨多模块联调的大改、需要 GPU/训练环境的实验、需要外部密钥的联网验证、需要人工判断产品取舍的决策。

大任务拆成若干可独立验收的小任务串行派发，比一次性丢一个大任务可靠得多。
