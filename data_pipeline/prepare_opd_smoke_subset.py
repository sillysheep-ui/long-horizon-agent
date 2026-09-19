#!/usr/bin/env python3
"""Create a deterministic short-query subset from the training-only OPD pool."""

import json
from pathlib import Path


SOURCE = Path("data/final/teacher_rl_pool_protocol_v2.jsonl")
OUTPUT = Path("data/final/opd_smoke_short_queries_20260821.jsonl")
INDICES = (10, 14, 25, 29, 53, 71)


def main():
    selected = []
    wanted = set(INDICES)
    with SOURCE.open(encoding="utf-8") as source:
        for index, line in enumerate(source):
            if index in wanted:
                row = json.loads(line)
                row["opd_smoke_source_index"] = index
                selected.append(row)
    found = {row["opd_smoke_source_index"] for row in selected}
    if found != wanted:
        raise RuntimeError(f"missing source rows: {sorted(wanted - found)}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as output:
        for row in selected:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(selected)} training-only rows to {OUTPUT}")


if __name__ == "__main__":
    main()
