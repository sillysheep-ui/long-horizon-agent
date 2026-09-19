#!/usr/bin/env python3
"""Build auditable short/medium prompt buckets for the multi-turn OPD pilot.

Only the system prompt and the first user request are retained. Assistant/tool
turns from the source trajectories are deliberately discarded because rollout
must remain on-policy. Exact requests present in the held-out test set are
excluded.
"""

import argparse
import hashlib
import json
import random
import re
from pathlib import Path


COMPLEXITY_TERMS = (
    "途经", "依次", "路线", "行程", "预算", "住宿", "餐饮", "高铁", "自驾",
    "多天", "三天", "两天", "一天", "周末", "亲子", "景点",
)


def normalize(text):
    return re.sub(r"\s+", "", str(text)).strip().lower()


def messages_of(row):
    return row.get("messages") or row.get("conversations") or []


def first_content(messages, role):
    return next((str(message.get("content", "")) for message in messages if message.get("role") == role), "")


def classify(query):
    complexity = sum(term in query for term in COMPLEXITY_TERMS)
    if len(query) <= 40 and complexity <= 1:
        return "short"
    if len(query) <= 80 and complexity <= 3:
        return "medium"
    return "long"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_test_queries(path):
    if not path.is_file():
        return set()
    queries = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            query = first_content(messages_of(row), "user")
            if query:
                queries.add(normalize(query))
    return queries


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def interleave(short_rows, medium_rows):
    """Interleave approximately 2 short prompts for every medium prompt."""
    result = []
    short_index = medium_index = 0
    while short_index < len(short_rows) or medium_index < len(medium_rows):
        for _ in range(2):
            if short_index < len(short_rows):
                result.append(short_rows[short_index])
                short_index += 1
        if medium_index < len(medium_rows):
            result.append(medium_rows[medium_index])
            medium_index += 1
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/final/rl.jsonl")
    parser.add_argument("--held-out", default="data/final/test_final.jsonl")
    parser.add_argument("--output-dir", default="data/final/opd_pilot_20260822")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    source = Path(args.source)
    held_out = Path(args.held_out)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    test_queries = load_test_queries(held_out)
    seen = set()
    buckets = {"short": [], "medium": [], "long": []}
    excluded_test_overlap = 0

    with source.open(encoding="utf-8") as handle:
        for source_index, line in enumerate(handle):
            row = json.loads(line)
            messages = messages_of(row)
            system = first_content(messages, "system")
            query = first_content(messages, "user")
            key = normalize(query)
            if not system or not query or key in seen:
                continue
            seen.add(key)
            if key in test_queries:
                excluded_test_overlap += 1
                continue
            bucket = classify(query)
            prompt_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            buckets[bucket].append({
                "id": f"opd-pilot-{prompt_id}",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": query},
                ],
                "opd_bucket": bucket,
                "source_index": source_index,
            })

    rng = random.Random(args.seed)
    for rows in buckets.values():
        rng.shuffle(rows)
    mixed = interleave(buckets["short"], buckets["medium"])

    paths = {
        "short": output_dir / "train_short.jsonl",
        "medium": output_dir / "train_medium.jsonl",
        "mixed": output_dir / "train_mixed.jsonl",
        "excluded_long": output_dir / "excluded_long.jsonl",
    }
    write_jsonl(paths["short"], buckets["short"])
    write_jsonl(paths["medium"], buckets["medium"])
    write_jsonl(paths["mixed"], mixed)
    write_jsonl(paths["excluded_long"], buckets["long"])

    manifest = {
        "source": str(source),
        "source_sha256": sha256(source),
        "held_out": str(held_out),
        "held_out_sha256": sha256(held_out) if held_out.is_file() else None,
        "seed": args.seed,
        "counts": {
            "short": len(buckets["short"]),
            "medium": len(buckets["medium"]),
            "mixed": len(mixed),
            "excluded_long": len(buckets["long"]),
            "excluded_exact_test_overlap": excluded_test_overlap,
        },
        "files": {name: {"path": str(path), "sha256": sha256(path)} for name, path in paths.items()},
        "policy": {
            "on_policy": "source assistant/tool turns are removed",
            "short": "query_chars<=40 and complexity_terms<=1",
            "medium": "query_chars<=80 and complexity_terms<=3",
            "mixed_order": "two short followed by one medium when available",
            "long": "excluded from the 50-step pilot",
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(manifest_path)


if __name__ == "__main__":
    main()
