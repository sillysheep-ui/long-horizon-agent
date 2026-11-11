import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "codex_task.py"
spec = importlib.util.spec_from_file_location("codex_task", SCRIPT_PATH)
codex_task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex_task)


class CodexTaskTest(unittest.TestCase):
    def test_parse_status_preserves_status_columns_and_rename_destination(self):
        for line, expected in (
            (" M x", {"x": " M"}),
            ("?? y", {"y": "??"}),
            ("A  z", {"z": "A "}),
            ("R  a -> b", {"b": "R "}),
        ):
            with self.subTest(line=line):
                self.assertEqual(codex_task.parse_status(line), expected)

        self.assertEqual(
            codex_task.parse_status(" M x\n?? y\nA  z\nR  a -> b\n"),
            {"x": " M", "y": "??", "z": "A ", "b": "R "},
        )

    def test_pick_sandbox_auto_with_outer_sandbox(self):
        with patch.dict(os.environ, {"DSH_SHELL": "1"}, clear=True):
            self.assertEqual(codex_task.pick_sandbox("auto", False), "danger-full-access")

    def test_pick_sandbox_auto_without_outer_sandbox(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codex_task.pick_sandbox("auto", False), "workspace-write")

    def test_pick_sandbox_rejects_unsandboxed_without_permission(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "--allow-unsandboxed"):
                codex_task.pick_sandbox("danger-full-access", False)

    def test_pick_sandbox_allows_explicit_unsandboxed_with_outer_sandbox(self):
        with patch.dict(os.environ, {"DSH_SHELL": "1"}, clear=True):
            self.assertEqual(
                codex_task.pick_sandbox("danger-full-access", False),
                "danger-full-access",
            )

    def test_build_prompt_review_requires_read_only_and_severity_levels(self):
        for uncommitted in (False, True):
            with self.subTest(uncommitted=uncommitted):
                prompt = codex_task.build_prompt(
                    "review", "检查错误处理", SCRIPT_PATH.parent.parent, uncommitted
                )
                self.assertIn("不要修改任何文件", prompt)
                self.assertIn("阻断 / 重要 / 次要", prompt)
                self.assertIn("文件:行号", prompt)
                self.assertIn("检查错误处理", prompt)
                self.assertIn("git diff --stat && git diff", prompt)
                if uncommitted:
                    self.assertIn("git status --porcelain", prompt)
                else:
                    self.assertNotIn("git status --porcelain", prompt)

    def test_parse_events_extracts_usage_commands_failures_and_last_message(self):
        usage = {"input_tokens": 120, "cached_input_tokens": 40, "output_tokens": 30}
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "开始检查"}},
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "git status --short", "exit_code": 0},
            },
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "python3 -m pytest", "exit_code": 2},
            },
            {"type": "item.completed", "item": {"type": "agent_message", "text": "检查完成"}},
            {"type": "turn.completed", "usage": usage},
        ]
        raw = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)

        result = codex_task.parse_events(raw)

        self.assertEqual(result["usage"], usage)
        self.assertEqual(
            result["commands"],
            ["git status --short  -> exit=0", "python3 -m pytest  -> exit=2"],
        )
        self.assertEqual(result["errors"], ["python3 -m pytest  -> exit=2"])
        self.assertEqual(result["last_message"], "检查完成")


    def test_build_prompt_plan_is_read_only_and_schema_bound(self):
        prompt = codex_task.build_prompt(
            "plan", "重构评测聚合", SCRIPT_PATH.parent.parent, False
        )
        self.assertIn("不要修改任何文件", prompt)
        self.assertIn("JSON Schema", prompt)
        self.assertIn("重构评测聚合", prompt)
        self.assertNotIn("阻断 / 重要 / 次要", prompt)

    def test_parse_plan_accepts_plain_json(self):
        plan = {
            "goal": "g",
            "assumptions": [],
            "steps": [{"id": "1", "action": "a", "files": ["x.py"], "acceptance": "ok"}],
            "risks": [],
            "out_of_scope": [],
        }
        parsed, error = codex_task.parse_plan(json.dumps(plan, ensure_ascii=False))
        self.assertEqual(error, "")
        self.assertEqual(parsed, plan)

    def test_parse_plan_tolerates_surrounding_text(self):
        plan = {"goal": "g", "steps": [{"id": "1", "action": "a", "files": [], "acceptance": "ok"}]}
        raw = "计划如下：\n```json\n" + json.dumps(plan) + "\n```\n以上。"
        parsed, error = codex_task.parse_plan(raw)
        self.assertEqual(error, "")
        self.assertEqual(parsed, plan)

    def test_parse_plan_rejects_invalid_or_step_less_payload(self):
        parsed, error = codex_task.parse_plan("不是 JSON")
        self.assertIsNone(parsed)
        self.assertIn("JSON", error)

        parsed, error = codex_task.parse_plan('{"goal": "g", "steps": []}')
        self.assertIsNone(parsed)
        self.assertIn("steps", error)

        parsed, error = codex_task.parse_plan("")
        self.assertIsNone(parsed)
        self.assertIn("没有返回", error)


if __name__ == "__main__":
    unittest.main()
