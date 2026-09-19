from data_pipeline.build_agent_prm_dataset import build_examples


def test_build_examples_extracts_stepwise_state_and_actions():
    record = {
        "id": "demo",
        "conversations": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "帮我规划北京一日游"},
            {"role": "assistant", "content": "<think>x</think><tool_call>{'name':'search'}</tool_call>"},
            {"role": "user", "content": "<tool_response>故宫开放</tool_response>"},
            {"role": "assistant", "content": "建议上午去故宫。"},
        ],
    }
    examples = build_examples(record, context_turns=6, text_limit=100)
    assert len(examples) == 2
    assert examples[0]["observed_action"] == "continue_tool"
    assert examples[1]["observed_action"] == "answer"
    assert examples[1]["state"]["query"] == "帮我规划北京一日游"
    assert examples[0]["outcome"]["total_tool_steps"] == 1
    assert examples[1]["outcome"]["is_terminal"] is True
