#!/usr/bin/env python3
"""TravelToolLoopScheduler 的无 GPU 回归测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import os
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('TRAVEL_AGENTIC_RL_ROOT', str(ROOT))
PLUGIN = ROOT / 'ms-swift/examples/train/grpo/plugin/tooluse_multi_turn_scheduler.py'


def load_plugin():
    spec = importlib.util.spec_from_file_location('travel_tool_loop_scheduler_under_test', PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTool:
    async def call(self, arguments):
        return f"FAKE_TOOL_RESULT:{arguments['query'][0]}"


class FakeEngine:
    def __init__(self, choices):
        self.choices = list(choices)
        self.tokenizer = SimpleNamespace()

    async def infer_async(self, infer_request, request_config, **kwargs):
        from swift.infer_engine.protocol import ChatCompletionResponse, UsageInfo
        return ChatCompletionResponse(
            model='fake-model',
            choices=[self.choices.pop(0)],
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def choice(content, token_ids, finish_reason='stop', tool_calls=None):
    from swift.infer_engine.protocol import ChatCompletionResponseChoice, ChatMessage
    return ChatCompletionResponseChoice(
        index=0,
        message=ChatMessage(role='assistant', content=content, tool_calls=tool_calls),
        token_ids=list(token_ids),
        finish_reason=finish_reason,
        logprobs=None,
    )


def request(messages):
    return SimpleNamespace(messages=messages, data_dict={}, uuid='unit-test')


def test_structured_tool_call_object_and_dict(module):
    scheduler = module.TravelToolLoopScheduler(max_turns=3)
    object_call = SimpleNamespace(function=SimpleNamespace(name='search', arguments='{"query":["A"]}'))
    dict_call = {'function': {'name': 'search', 'arguments': '{"query":["B"]}'}}
    response = choice('', [1], tool_calls=[object_call, dict_call])
    calls = scheduler._structured_tool_calls_from_choice(response)
    assert calls == [
        {'name': 'search', 'arguments': {'query': ['A']}},
        {'name': 'search', 'arguments': {'query': ['B']}},
    ]


def test_project_utils_wins_over_cli_module(module):
    original = sys.modules.get('utils')
    try:
        scheduler = module.TravelToolLoopScheduler(max_turns=3)
        assert (module.Path(scheduler.tools_dir).resolve().parent / 'utils' / '__init__.py').is_file()
        # Reproduce ms-swift importing its CLI helper after scheduler init.
        sys.modules['utils'] = types.ModuleType('utils')
        scheduler._activate_project_utils()
        assert 'utils' not in sys.modules
        assert sys.path[0] == str(module.Path(scheduler.tools_dir).resolve().parent)
    finally:
        sys.modules.pop('utils', None)
        if original is not None:
            sys.modules['utils'] = original


def test_unknown_tool_is_not_counted_valid(module):
    scheduler = module.TravelToolLoopScheduler(max_turns=3)
    original = module.load_tool
    module.load_tool = lambda tools_dir, name: None
    try:
        result = asyncio.run(scheduler._execute_tool_calls(
            [{'name': 'not_a_tool', 'arguments': {}}], sample_idx='x', turn=1))
    finally:
        module.load_tool = original
    assert result['total_tool_calls'] == 1
    assert result['valid_tool_calls'] == 0
    assert result['tool_messages'][0]['content'].startswith('TOOL_ERROR: unsupported tool')


def test_full_rollout_masks_environment_tokens(module):
    tool_text = '<tool_call>\n{"name":"search","arguments":{"query":["杭州天气"]}}\n</tool_call>'
    final_text = '<answer>已根据工具结果回答。</answer>'
    engine = FakeEngine([
        choice(tool_text, [10, 11, 12]),
        choice(final_text, [20, 21]),
    ])
    scheduler = module.TravelToolLoopScheduler(infer_engine=engine, max_turns=3)
    original = module.load_tool
    module.load_tool = lambda tools_dir, name: FakeTool() if name == 'search' else None
    try:
        output = asyncio.run(scheduler.run(
            request([
                {'role': 'system', 'content': '你是旅行助手。最大可调用13轮工具'},
                {'role': 'user', 'content': '查杭州天气'},
            ]),
            SimpleNamespace(n=1),
        ))
    finally:
        module.load_tool = original

    assert output.response_token_ids == [[10, 11, 12], [20, 21]], output.response_token_ids
    assert output.response_loss_mask == [[1, 1, 1], [1, 1]], output.response_loss_mask
    assert all('FAKE_TOOL_RESULT' not in str(turn) for turn in output.response_token_ids)
    tool_messages = [m for m in output.messages if m.get('role') == 'tool']
    assert len(tool_messages) == 1
    assert tool_messages[0]['content'] == 'FAKE_TOOL_RESULT:杭州天气'
    assert output.rollout_infos['status'] == 'answer'
    assert output.rollout_infos['tool_calls'] == 1


def test_context_budget_compacts_only_environment_tokens(module):
    class CharTokenizer:
        @staticmethod
        def apply_chat_template(messages, tokenize=True, add_generation_prompt=True):
            return list(range(sum(len(str(message.get('content', ''))) for message in messages)))

    engine = FakeEngine([])
    engine.tokenizer = CharTokenizer()
    scheduler = module.TravelToolLoopScheduler(infer_engine=engine, max_turns=3)
    scheduler.max_context_tokens = 100
    scheduler.final_answer_reserve_tokens = 30
    scheduler.compacted_tool_max_chars = 10
    messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'question'},
        {'role': 'assistant', 'content': 'A' * 20},
        {'role': 'tool', 'content': 'T' * 80},
    ]
    state = scheduler._get_state(request(messages))
    assistant_before = messages[2]['content']

    scheduler._enforce_context_budget(messages, state)

    assert messages[2]['content'] == assistant_before
    assert messages[3]['content'].startswith('T' * 10)
    assert messages[3]['content'].endswith('...[truncated]')
    assert any(message.get('content', '').startswith('TOOL_FEEDBACK:') for message in messages)
    assert state['status'] == 'context_budget_force_answer'
    assert state['context_compactions'] == 1


def main():
    module = load_plugin()
    tests = [
        test_structured_tool_call_object_and_dict,
        test_project_utils_wins_over_cli_module,
        test_unknown_tool_is_not_counted_valid,
        test_full_rollout_masks_environment_tokens,
        test_context_budget_compacts_only_environment_tokens,
    ]
    for test in tests:
        test(module)
        print(f'PASS {test.__name__}')
    print(f'PASS all={len(tests)}')


if __name__ == '__main__':
    main()
