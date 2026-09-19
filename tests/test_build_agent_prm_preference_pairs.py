from data_pipeline.build_agent_prm_preference_pairs import to_pair


def test_pair_keeps_one_state_and_ranks_repair_above_risky_action():
    row = {
        "example_id": "x::cf_tool_failure",
        "trajectory_id": "x",
        "state": {"query": "测试", "history": []},
        "counterfactual_type": "tool_failure",
        "counterfactual_action": "answer",
        "synthetic_supervision": {"preferred_action": "replan", "reason": "工具失败"},
    }
    pair = to_pair(row)
    assert pair["chosen"]["action"] == "replan"
    assert pair["rejected"]["action"] == "answer"
    assert pair["requires_teacher_review"] is True
