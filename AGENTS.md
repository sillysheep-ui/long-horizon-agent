# AGENTS.md

本仓库的编码 Agent（Codex、DSH 等）工作约定。

## 版本维护

- 每完成一个可独立验收的任务就提交一次，不攒大提交；提交前跑 `python3 -m pytest tests -q`。
- 提交信息格式：`<type>: <中文摘要>`，type 取 `feat` / `fix` / `test` / `docs` / `chore` / `refactor`。
- 一次提交只做一件事；顺手修的问题单独提交。
- 开工前先 `git fetch --quiet`；远端领先就 `git pull --rebase` 再动工。
- 默认只在本地提交，`git push` 需用户明确同意。
- 不提交 `.codex-home/`、`.codex-runs/`、模型权重、日志和临时产物。

## 任务边界

- 大改先出计划（可用 `scripts/codex_task.py --mode plan`），再动手。
- 不擅自改 CI、依赖清单和训练脚本的默认参数。
- 改动涉及公开行为时同步更新 README 或对应 docs。
