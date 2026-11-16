"""检查 examples/ 下的脱敏训练样例：格式是否合法、是否还有敏感串。

用法：
    python3 scripts/check_examples.py
    python3 scripts/check_examples.py --path examples/sft_sample.jsonl
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

SECRET_PATTERNS = [
    ("api_key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}")),
    ("mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("landline", re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("home_path", re.compile(r"/(Users|root)/[^\s\"']+")),
]
PLACEHOLDERS = {"13800000000", "010-00000000", "user@example.com"}
ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


def load_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {number} 行不是合法 JSON：{exc}") from exc
    return records


def validate_records(records: list[dict[str, Any]]) -> list[str]:
    errors = []
    for index, record in enumerate(records, 1):
        if not record.get("id"):
            errors.append(f"第 {index} 条缺少 id")
        conversations = record.get("conversations")
        if not isinstance(conversations, list) or len(conversations) < 3:
            errors.append(f"第 {index} 条 conversations 少于 3 条消息")
            continue
        for message in conversations:
            if message.get("role") not in ALLOWED_ROLES:
                errors.append(f"第 {index} 条含未知 role：{message.get('role')}")
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                errors.append(f"第 {index} 条存在空 content")
    return errors


def scan_secrets(text: str) -> list[str]:
    findings = []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.findall(text):
            if match in PLACEHOLDERS:
                continue
            findings.append(f"{name}: {match[:40]}")
    return findings


def stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    roles = Counter()
    lengths = []
    for record in records:
        for message in record.get("conversations", []):
            roles[message.get("role")] += 1
            lengths.append(len(message.get("content") or ""))
    return {
        "records": len(records),
        "roles": dict(roles),
        "avg_chars": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        "max_chars": max(lengths) if lengths else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="examples/sft_sample.jsonl")
    args = parser.parse_args()

    path = Path(args.path)
    records = load_records(path)
    errors = validate_records(records)
    secrets = scan_secrets(path.read_text(encoding="utf-8"))
    summary = stats(records)

    print(f"样例：{path}")
    print(f"记录数：{summary['records']}")
    print(f"角色分布：{summary['roles']}")
    print(f"平均长度：{summary['avg_chars']} 字符，最长 {summary['max_chars']}")
    print(f"格式问题：{len(errors)}")
    for error in errors[:10]:
        print(f"  - {error}")
    print(f"敏感串：{len(secrets)}")
    for finding in secrets[:10]:
        print(f"  - {finding}")
    if errors or secrets:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
