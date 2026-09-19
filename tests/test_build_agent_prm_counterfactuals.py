from data_pipeline.build_agent_prm_counterfactuals import make_variant


def _example(action="continue_tool", terminal=False):
    return {
        "example_id": "x::step_0",
        "observed_action": action,
        "state": {"history": [{"role": "user", "content": "<tool_response>有效证据</tool_response>"}]},
        "outcome": {"is_terminal": terminal},
        "teacher_label": None,
    }


def test_tool_failure_marks_evidence_unavailable_and_prefers_replan():
    variant = make_variant(_example(), "tool_failure")
    assert variant["data_source"] == "counterfactual"
    assert "工具调用失败" in variant["state"]["history"][0]["content"]
    assert variant["synthetic_supervision"]["preferred_action"] == "replan"


def test_premature_answer_only_comes_from_nonterminal_tool_step():
    assert make_variant(_example(), "premature_answer") is not None
    assert make_variant(_example(action="answer", terminal=True), "premature_answer") is None
