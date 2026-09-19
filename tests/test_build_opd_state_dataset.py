import json
import subprocess
import sys
from pathlib import Path


def test_build_state_dataset(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    rows = []
    for index in range(3):
        rows.append(
            {
                "id": f"t{index}",
                "clean_flag": "keep",
                "conversations": [
                    {"role": "system", "content": "格式 <answer>...</answer>"},
                    {"role": "user", "content": f"问题{index}"},
                    {"role": "assistant", "content": "<tool_call>{}</tool_call>"},
                    {"role": "user", "content": "<tool_response>证据</tool_response>"},
                    {"role": "assistant", "content": "<answer>结论</answer>"},
                ],
            }
        )
    source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    output = tmp_path / "out"
    script = Path(__file__).resolve().parents[1] / "data_pipeline" / "build_opd_state_dataset.py"
    subprocess.run(
        [sys.executable, str(script), "--input", str(source), "--output-dir", str(output), "--dev-trajectories", "1"],
        check=True,
    )
    train = [json.loads(line) for line in (output / "train.jsonl").open()]
    dev = [json.loads(line) for line in (output / "dev.jsonl").open()]
    assert len(train) == 4
    assert len(dev) == 2
    assert {row["trajectory_id"] for row in train}.isdisjoint({row["trajectory_id"] for row in dev})
    assert all(row["messages"][-1]["role"] == "assistant" for row in train + dev)
    assert all("<answer>...</answer>" not in row["messages"][0]["content"] for row in train + dev)
    assert {row["decision_type"] for row in train + dev} == {"tool_call", "final_answer"}
