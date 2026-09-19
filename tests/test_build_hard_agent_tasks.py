from data_pipeline.build_hard_agent_tasks import CASES, l3_task, l4_task


def test_hard_tasks_require_dynamic_decisions():
    l3 = l3_task(1, *CASES[0])
    l4 = l4_task(1, *CASES[0])
    assert l3["min_conditional_decisions"] == 1
    assert l4["min_conditional_decisions"] == 2
    assert l4["tier"].startswith("L4")
