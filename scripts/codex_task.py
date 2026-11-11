#!/usr/bin/env python3
"""把自包含的编码任务派给 Codex CLI（codex exec），并收集轨迹、用量和改动供人工审查。

这个脚本只负责「派单 + 取证」，不做自动合并：
  1. 以非交互方式启动 `codex exec`，注入任务说明；
  2. 把 Codex 的 JSONL 事件流、stderr、最终消息落盘；
  3. 记录任务前后 `git status` / `git diff`，生成 changes.patch 与 report.md；
  4. 打印一份 JSON 摘要，供调用方（人或上层 Agent）决定是否采纳。

典型用法：
    python3 scripts/codex_task.py "给 utils/text.py 补单元测试，只改 tests/ 下的文件"
    python3 scripts/codex_task.py --task-file /tmp/task.md --timeout 1800
    python3 scripts/codex_task.py --mode review --uncommitted
    python3 scripts/codex_task.py --mode plan --reasoning-effort xhigh "重构 eval 的评分聚合"

退出码：0 成功 / 1 Codex 失败 / 2 超时 / 3 环境或参数错误
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

RUNS_DIRNAME = ".codex-runs"
CODEX_HOME_DIRNAME = ".codex-home"
PLAN_SCHEMA_PATH = Path(__file__).resolve().with_name("codex_plan_schema.json")


def log(msg: str) -> None:
    print(f"[codex_task] {msg}", file=sys.stderr, flush=True)


def git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 只去掉行尾换行：`git status --porcelain` 首行以空格开头，
        # 用 strip() 会把状态位吃掉，后续按 line[3:] 解析就会截断文件名。
        return out.stdout.rstrip("\n")
    except Exception:
        return ""


def is_git_repo(path: Path) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree") == "true"


def resolve_repo(cd: str | None) -> Path:
    base = Path(cd).expanduser().resolve() if cd else Path.cwd()
    if not base.exists():
        raise SystemExit(f"[codex_task] 目录不存在: {base}")
    if is_git_repo(base):
        top = git(base, "rev-parse", "--show-toplevel")
        if top:
            return Path(top)
    return base


def ensure_codex_home(repo: Path) -> tuple[Path, bool]:
    """返回 (CODEX_HOME, 是否为本脚本创建的本地目录)。

    DSH 沙箱默认只允许写工作区，而 Codex 需要可写的状态库目录；
    因此把 CODEX_HOME 放到仓库内的 .codex-home，并用软链接复用 ~/.codex/auth.json。
    """
    explicit = os.environ.get("CODEX_HOME")
    if explicit:
        home = Path(explicit).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True)
        return home, False

    home = repo / CODEX_HOME_DIRNAME
    home.mkdir(parents=True, exist_ok=True)
    auth_link = home / "auth.json"
    user_auth = Path.home() / ".codex" / "auth.json"
    if not auth_link.exists() and user_auth.exists():
        try:
            auth_link.symlink_to(user_auth)
        except OSError:
            shutil.copy2(user_auth, auth_link)
            auth_link.chmod(0o600)
    if not auth_link.exists():
        log(f"警告: 未找到 Codex 登录凭证 {user_auth}，请先运行 `codex login`")
    return home, True


def pick_sandbox(requested: str, allow_unsandboxed: bool) -> str:
    """选择 Codex 内层沙箱。

    Codex 自带的 seatbelt 沙箱嵌套在 DSH 的沙箱里无法初始化，命令会全部失败，
    所以在外层已有沙箱（DSH_SHELL=1）时改用 danger-full-access，由外层兜底。
    脱离外层沙箱时必须显式 --allow-unsandboxed 才允许这么干。
    """
    outer_sandbox = os.environ.get("DSH_SHELL") == "1"

    if requested != "auto":
        if requested == "danger-full-access" and not (outer_sandbox or allow_unsandboxed):
            raise SystemExit(
                "[codex_task] 拒绝在无外层沙箱的情况下使用 danger-full-access；"
                "请改用 workspace-write，或加 --allow-unsandboxed 明确承担风险"
            )
        return requested

    if outer_sandbox:
        return "danger-full-access"
    return "workspace-write"


def build_prompt(mode: str, prompt: str, repo: Path, uncommitted: bool) -> str:
    if mode == "plan":
        body = prompt.strip() or "先读代码了解现状，再给出下一步实施计划。"
        return (
            "你在做实施规划，不要修改任何文件，也不要运行会改动仓库的命令。\n"
            "先读代码确认现状（真实函数名、路径、现有测试），再输出计划；\n"
            "计划要能让另一位工程师不追问就直接照做。\n"
            "只输出一个符合给定 JSON Schema 的 JSON 对象，不要代码块、不要额外解释。\n\n"
            f"目标：{body}"
        )

    if mode != "review":
        return prompt

    diff_cmd = "git diff --stat && git diff"
    if uncommitted:
        diff_cmd = "git status --porcelain && " + diff_cmd
    review_prompt = prompt.strip() or "审查当前改动，按严重程度列出问题。"
    return (
        "你在做代码审查，不要修改任何文件。\n"
        f"审查范围：{diff_cmd}\n"
        "要求：\n"
        "1. 只有真实缺陷才报，不要为凑数挑风格问题；\n"
        "2. 每条给出 文件:行号、问题、为什么是问题、建议改法；\n"
        "3. 按 阻断 / 重要 / 次要 分级；没有问题的部分直接说没问题。\n\n"
        f"额外关注点：{review_prompt}"
    )


def terminate(proc: subprocess.Popen) -> None:
    """结束 Codex 及其子进程组；进程已退出时无副作用。"""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def run_codex(
    repo: Path,
    home: Path,
    prompt: str,
    sandbox: str,
    model: str | None,
    ephemeral: bool,
    timeout: float,
    run_dir: Path,
    effort: str | None = None,
    output_schema: Path | None = None,
) -> dict[str, Any]:
    transcript = run_dir / "transcript.jsonl"
    stderr_log = run_dir / "stderr.log"
    last_msg = run_dir / "last_message.md"

    cmd = [
        "codex",
        "exec",
        "-C",
        str(repo),
        "-s",
        sandbox,
        "--json",
        "-o",
        str(last_msg),
    ]
    if model:
        cmd += ["-m", model]
    if effort:
        # 以 TOML 片段形式覆盖配置；值必须带引号，否则会被当成标识符解析。
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    if output_schema:
        # 让 Codex 按 schema 产出结构化 JSON（plan 模式依赖它）
        cmd += ["--output-schema", str(output_schema)]
    if ephemeral:
        cmd.append("--ephemeral")
    if not is_git_repo(repo):
        cmd.append("--skip-git-repo-check")
    cmd.append(prompt)

    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)

    log(f"启动 Codex（sandbox={sandbox}, timeout={int(timeout)}s）")
    started = time.monotonic()
    timed_out = False
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            log(f"超时 {int(timeout)}s，终止进程组")
            terminate(proc)
            try:
                # 超时后只再等固定时间：若后代进程另起会话并占着管道，
                # 无限期的第二次 communicate() 会把整个脚本挂住。
                stdout, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = "", ""
    finally:
        # 覆盖 Ctrl+C / SIGTERM 等异常路径，避免 Codex 留在后台继续改仓库
        terminate(proc)

    duration = time.monotonic() - started
    transcript.write_text(stdout or "", encoding="utf-8")
    stderr_log.write_text(stderr or "", encoding="utf-8")

    events = parse_events(stdout or "")
    final = last_msg.read_text(encoding="utf-8").strip() if last_msg.exists() else ""
    if not final:
        final = events["last_message"]

    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_s": round(duration, 1),
        "final_message": final,
        "usage": events["usage"],
        "commands": events["commands"],
        "errors": events["errors"],
    }


def parse_events(raw: str) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    commands: list[str] = []
    errors: list[str] = []
    last_message = ""

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        item = evt.get("item") or {}
        if etype == "item.completed":
            if item.get("type") == "agent_message":
                last_message = item.get("text", "") or last_message
            elif item.get("type") == "command_execution":
                cmd = item.get("command") or ""
                code = item.get("exit_code")
                if cmd:
                    commands.append(f"{cmd}  -> exit={code}")
                if code not in (0, None):
                    errors.append(f"{cmd}  -> exit={code}")
        elif etype == "turn.completed":
            usage = evt.get("usage", {}) or {}
        elif etype == "error":
            msg = evt.get("message") or evt.get("error") or ""
            if msg:
                errors.append(str(msg))

    return {
        "usage": usage,
        "commands": commands,
        "errors": errors,
        "last_message": last_message,
    }


def parse_plan(raw: str) -> tuple[dict[str, Any] | None, str]:
    """解析 plan 模式产出的 JSON 计划，返回 (计划, 错误说明)。"""
    text = (raw or "").strip()
    if not text:
        return None, "Codex 没有返回计划内容"

    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])  # 容忍外面包了废话或代码块标记

    for candidate in candidates:
        try:
            plan = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(plan, dict):
            break
        if not isinstance(plan.get("steps"), list) or not plan["steps"]:
            return None, "计划 JSON 缺少非空 steps 数组"
        return plan, ""
    return None, "计划不是合法 JSON"


def snapshot(repo: Path) -> dict[str, str]:
    return {
        "status": git(repo, "status", "--porcelain"),
        "diff_stat": git(repo, "diff", "--stat"),
        "diff": git(repo, "diff"),
    }


def parse_status(porcelain: str) -> dict[str, str]:
    """把 `git status --porcelain` 解析成 {路径: 两字符状态码}。"""
    result: dict[str, str] = {}
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        if " -> " in path:  # 重命名：取新路径
            path = path.split(" -> ", 1)[1]
        result[path.strip()] = code
    return result


def write_report(
    run_dir: Path,
    repo: Path,
    prompt: str,
    sandbox: str,
    model: str | None,
    result: dict[str, Any],
    before: dict[str, str],
    after: dict[str, str],
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before_map = parse_status(before["status"])
    after_map = parse_status(after["status"])

    # 按「路径 -> 状态」逐项对比：任务前就已脏的文件如果被继续修改，
    # 只做文件名集合差集会漏掉，这里的比较能把这类改动也报出来。
    changed = sorted(
        path
        for path, code in after_map.items()
        if not code.startswith("??") and before_map.get(path) != code
    )
    untracked = sorted(
        path for path, code in after_map.items() if code == "??" and path not in before_map
    )
    new_diff = after["diff"] if after["diff"] != before["diff"] else ""
    if new_diff:
        (run_dir / "changes.patch").write_text(new_diff, encoding="utf-8")

    usage = result.get("usage") or {}
    lines = [
        "# Codex 任务报告",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 仓库：`{repo}`",
        f"- 模型：{model or '(Codex 默认)'}",
        f"- 内层沙箱：{sandbox}",
        f"- 退出码：{result['exit_code']}" + ("（超时被终止）" if result["timed_out"] else ""),
        f"- 耗时：{result['duration_s']}s",
        f"- Tokens：input={usage.get('input_tokens', '-')} "
        f"cached={usage.get('cached_input_tokens', '-')} "
        f"output={usage.get('output_tokens', '-')}",
        f"- 执行命令数：{len(result['commands'])}",
        "",
        "## 任务",
        "",
        "```text",
        prompt.strip(),
        "```",
        "",
        "## Codex 最终回复",
        "",
        result["final_message"] or "（无）",
        "",
    ]
    if plan:
        lines += ["## 计划", "", f"**目标**：{plan.get('goal', '')}", ""]
        for step in plan["steps"]:
            files = "、".join(step.get("files") or []) or "（未指定）"
            lines += [
                f"### {step.get('id', '?')}. {step.get('action', '')}",
                "",
                f"- 文件：{files}",
                f"- 验收：{step.get('acceptance', '')}",
                "",
            ]
        if plan.get("assumptions"):
            lines += ["**假设**：", *[f"- {a}" for a in plan["assumptions"]], ""]
        if plan.get("risks"):
            lines += ["**风险**：", *[f"- {r}" for r in plan["risks"]], ""]
        if plan.get("out_of_scope"):
            lines += ["**不做**：", *[f"- {o}" for o in plan["out_of_scope"]], ""]

    lines += [
        "## 改动文件",
        "",
        *([f"- `{p}`" for p in changed] or ["（无已跟踪文件改动）"]),
    ]
    if untracked:
        lines += ["", "## 新增未跟踪文件", "", *[f"- `{p}`" for p in untracked]]
    if result["errors"]:
        lines += ["", "## 执行中的异常", "", *[f"- `{e}`" for e in result["errors"][:20]]]
    if new_diff:
        lines += ["", "## diff 统计", "", "```text", after["diff_stat"], "```"]

    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"changed_files": changed, "untracked_files": untracked, "has_patch": bool(new_diff)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把自包含的编码任务派给 Codex CLI，并落盘轨迹与改动供审查"
    )
    parser.add_argument("prompt", nargs="*", help="任务说明（也可用 --task-file / stdin）")
    parser.add_argument("--task-file", help="从文件读取任务说明")
    parser.add_argument("--cd", help="工作目录，默认当前目录所在的 git 仓库根")
    parser.add_argument("--model", "-m", help="覆盖 Codex 模型，例如 gpt-5-codex")
    parser.add_argument(
        "--sandbox",
        default="auto",
        choices=["auto", "read-only", "workspace-write", "danger-full-access"],
        help="Codex 内层沙箱；auto 在外层已有沙箱时用 danger-full-access，否则 workspace-write",
    )
    parser.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="在无外层沙箱时允许 Codex 使用 danger-full-access（危险）",
    )
    parser.add_argument(
        "--mode",
        choices=["task", "review", "plan"],
        default="task",
        help="task=执行任务；review=只读审查；plan=只读规划并按 schema 输出 JSON 计划",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        help="覆盖 Codex 推理强度；plan 模式默认 xhigh",
    )
    parser.add_argument(
        "--plan-schema",
        help=f"plan 模式的 JSON Schema，默认 {PLAN_SCHEMA_PATH.name}",
    )
    parser.add_argument("--uncommitted", action="store_true", help="review 模式审查未提交改动")
    parser.add_argument("--timeout", type=float, default=900, help="超时秒数，默认 900")
    parser.add_argument("--ephemeral", action="store_true", help="不保留 Codex 会话文件")
    parser.add_argument("--out-dir", help=f"运行产物目录，默认 <repo>/{RUNS_DIRNAME}/<时间戳>")
    args = parser.parse_args(argv)

    if shutil.which("codex") is None:
        log("错误: 未找到 codex 命令，请先 npm i -g @openai/codex")
        return 3

    if args.task_file:
        prompt = Path(args.task_file).expanduser().read_text(encoding="utf-8")
    elif args.prompt:
        prompt = " ".join(args.prompt)
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        log("错误: 需要任务说明（位置参数 / --task-file / stdin）")
        return 3

    if not prompt.strip():
        log("错误: 任务说明为空")
        return 3

    repo = resolve_repo(args.cd)
    home, _ = ensure_codex_home(repo)
    sandbox = pick_sandbox(args.sandbox, args.allow_unsandboxed)
    prompt = build_prompt(args.mode, prompt, repo, args.uncommitted)

    # plan 模式默认用最高推理强度，并强制结构化输出；其余模式不改变默认行为
    effort = args.reasoning_effort or ("xhigh" if args.mode == "plan" else None)
    output_schema: Path | None = None
    if args.mode == "plan":
        schema_path = (
            Path(args.plan_schema).expanduser().resolve()
            if args.plan_schema
            else PLAN_SCHEMA_PATH
        )
        if not schema_path.exists():
            log(f"错误: 找不到 plan schema: {schema_path}")
            return 3
        output_schema = schema_path

    run_dir = (
        Path(args.out_dir).expanduser().resolve()
        if args.out_dir
        else repo / RUNS_DIRNAME / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    before = snapshot(repo)
    result = run_codex(
        repo=repo,
        home=home,
        prompt=prompt,
        sandbox=sandbox,
        model=args.model,
        ephemeral=args.ephemeral,
        timeout=args.timeout,
        run_dir=run_dir,
        effort=effort,
        output_schema=output_schema,
    )
    after = snapshot(repo)

    plan: dict[str, Any] | None = None
    plan_error = ""
    if args.mode == "plan":
        plan, plan_error = parse_plan(result["final_message"])
        if plan:
            (run_dir / "plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
            log(f"警告: {plan_error}")

    changes = write_report(
        run_dir, repo, prompt, sandbox, args.model, result, before, after, plan=plan
    )

    # plan 模式没拿到合法计划等于白跑，视为失败
    ok = result["exit_code"] == 0 and not result["timed_out"]
    if args.mode == "plan" and plan is None:
        ok = False

    summary = {
        "ok": ok,
        "mode": args.mode,
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "duration_s": result["duration_s"],
        "sandbox": sandbox,
        "model": args.model or "default",
        "reasoning_effort": effort or "default",
        "run_dir": str(run_dir),
        "last_message": result["final_message"],
        "plan": plan,
        "usage": result["usage"],
        "commands_run": len(result["commands"]),
        "errors": result["errors"][:10],
        **changes,
    }
    if plan_error:
        summary["plan_error"] = plan_error
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if result["timed_out"]:
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
