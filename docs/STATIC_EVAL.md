# 静态评测模式（STATIC_EVAL）

沙盒不是“联网模式的廉价替代”，而是一套独立的静态评测环境：真值只来自快照，外部检索被禁用。

## 怎么开

```bash
# 静态套件（80 条 test；20 条 holdout 冻结，发布前才跑）
python3 -m evaluation.product_eval --static --spec evaluation/specs/static.yaml --skip-judge
```

等价于 `STATIC_EVAL=1 POI_PROVIDER=sandbox`，并额外：

- 只跑 `pool=sandbox` 的用例（显式传 `--pool live` 会直接报错）；
- 网页搜索返回 `unavailable`（reason=“静态评测禁止外部检索”），不会发起外呼；
- 报告写入数据指纹：用例文件哈希、约束集版本、沙盒 `sha256/downloaded_at`。

模型 API 仍是唯一网络依赖（生成与裁判）；要彻底离线，需要本地小模型或去掉裁判。

## 判分口径

每条检查分三类：

| 类别 | 含义 | 缺数据时 |
|---|---|---|
| `static` | 只依赖行程本身（天数、时间、几何、结构） | 不需要快照字段 |
| `snapshot` | 依赖快照可选字段（营业时间、价格） | 记 `unknown`，**不进通过率分母** |
| `case` | 该用例本来不涉及（无锚定/无点名/无餐） | 记 `not_applicable`，不算缺数据 |

报告里每条检查都给出：`判了 / 未知 / 通过率`，并汇总一行“静态可判覆盖”。这样 41 条沙盒不会再用“全过”掩盖 `opening_hours` 被跳过。

## 套件结构

`evaluation/static_cases.json` 共 100 条，10 城 × 10 条：

| 划分 | 条数 | 用途 |
|---|---|---|
| `test` | 80 | 日常回归与提示词迭代 |
| `holdout` | 20 | 冻结，发布前才跑 |

用例按“点名景点 / 只逛老街 / 亲子 / 带父母 / 预算紧 / 夜游”分主题，覆盖快照缺营业时间
（老街类 POI 的 `00:00–24:00` 占位）与缺价格两类分支。

## 数据质量门禁

跑静态套件前会先审计快照：用例依赖的城市必须有足够景点、坐标覆盖 ≥95%。审计结果写进报告
（三张表的行数、坐标/营业时间/价格覆盖），门禁不通过直接退出。当前快照：

| 表 | 行数 | 坐标 | 营业时间 | 价格 |
|---|---|---|---|---|
| attractions | 3413 | 100% | 67.4% | 27.8% |
| restaurants | 4655 | 100% | 95.3% | 100% |
| accommodations | 3866 | 100% | 0% | 100% |

## 当前边界

- 100 条用例、10 城；难度按 easy/medium/hard 分层，但仍偏 easy；
- 快照缺陷（地址 0%、景点价格 27.8%、景点营业时间 67.4%）会体现为 `unknown`，不是通过；
- 实时营业、排队、临时闭馆属于动态能力，不进静态套件，另做 dynamic smoke；
- judge 仍是外部模型，rubric 不进静态主数字。

## 本地裁判（可选）

裁判可以走本地 OpenAI 兼容服务，不依赖外部 API：

```bash
# 1) 起服务（模型目录必须是真实文件，不能是 HF 缓存里的符号链接）
scripts/serve_local_model.sh /path/to/Qwen3-4B

# 2) 跑静态套件，加分裁判
LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1 \
python3 -m evaluation.product_eval --static --spec evaluation/specs/static.yaml \
  --judge-backend local --judge-model /path/to/Qwen3-4B
```

- 生成仍走 `MODEL_PROFILE` 路由；要全链路离线，再加 `MODEL_PROFILE=local_only`（质量会降）。
- 本地服务默认 `max_tokens=4096`；小模型开思考会截断 JSON，可加
  `MLX_CHAT_TEMPLATE_ARGS='{"enable_thinking": false}'`。
- **实测结论（2026-11-26）**：0.8B 5 条全 rubric 1.0，偏松；4B（Qwen3-4B-Instruct-2507-4bit）
  在 5 条正常样例上 rubric 0.98、在 3 条已知弱样例上 0.867，看起来有区分度，但**抓不到注入的编造价格**：
  把“门票改成 999 元/人（已核实）”后，4B 仍判 rubric 1.0、fabrication=false；同一输入 API 裁判判 0.5、
  fabrication=true。结论：本地 4B 只用于离线冒烟/回归，不用于对外分数；rubric 校准应针对 API 裁判。

## 人工校准 rubric

裁判分不能直接信，先导出一份人工评分清单：

```bash
python3 -m evaluation.calibration export \
  --records /tmp/calib50/records.json \
  --cases evaluation/static_cases.json \
  --out /tmp/calib50/review
# 填 review.csv 的 human_score（1–5）
python3 -m evaluation.calibration compare \
  --records /tmp/calib50/records.json --labels /tmp/calib50/review/review.csv
```

`compare` 输出一致率（人工 ≥4 与裁判 rubric ≥0.7 是否一致）、Cohen's kappa 与均值差。
当前只有 Qwen3.5-0.8B 与 Qwen3-4B 的临时结果，样本未标完，不对外引用。

## 下一步

1. 补多城与修订类静态用例（当前以单城为主）；
2. 换更强的本地裁判并做人工校准，再把 rubric 纳入静态口径；
3. 动态能力单独做 smoke，不与静态套件混算。

## 首轮结果（2026-11-26，80 条 test）

| 全过 | micro | macro | 静态可判覆盖 | 未知项次 | 耗时 |
|---|---|---|---|---|---|
| 76/80（0.950） | 0.995 | 0.950 | 99.2% | 11 | 15.25s |

失败 4 条：`static-hangzhou-08` 编造价格、`static-chengdu-03` 节奏过满（density/day_load/periods/free_time）、
`static-chongqing-08` 与 `static-shenzhen-08` 营业时间冲突。未知集中在“只逛老街”的用例（营业时间占位）
与无数字价格的行程，说明 unknown 口径开始生效。

20 条 holdout 已冻结，发布前单独跑：

```bash
python3 -m evaluation.product_eval --static --cases evaluation/static_cases.json --split holdout --skip-judge
```

## 旧首轮结果（2026-11-26，59 条）

| 范围 | 全过 | micro | 未知 | 耗时 |
|---|---|---|---|---|
| 全部沙盒 59 条（含 dev/holdout） | 53/59（0.898） | 0.991 | 0 | 16.6s |
| test 41 条 | 36/41（0.878） | 0.991 | 0 | — |
| dev 7 条 | 7/7 | 1.0 | 0 | — |
| holdout 11 条 | 10/11 | 0.985 | 0 | — |

- 相比用餐修复前 test 25/41（0.61），本次 36/41，主要来自 `no_overlap` 的确定性挪餐。
- 本轮 `unknown` 为 0：抽到的证据都带营业时间，说明这批用例还没覆盖“快照缺字段”的分支；需要补用例才能检验 unknown 口径。
- 仍有一类“假通过”：`no_fabricated_cost` 在活动没有数字价格时直接通过，而不是记未知；下一步要让价格不可核验时记 unknown。
