"""pytest 配置。

test_travel_multi_turn_scheduler.py 是需要 ms-swift 环境的独立回归脚本，
由 scripts/run_opd_multiturn_pilot_8x_rtx6000d.sh 直接调用，因此不参与
pytest 收集，避免在本机无 swift 环境时中断整个测试套件。
"""

collect_ignore = ['test_travel_multi_turn_scheduler.py']
