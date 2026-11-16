# 脱敏训练样例

这里放了一小份**可以直接跑通格式**的 SFT 样例，用来回答两个问题：

1. 训练数据长什么样？
2. 没有完整数据集时，能不能先跑一遍流程？

## 内容

- `sft_sample.jsonl`：60 条，从完整训练集（850 条）按固定间隔抽取
- 格式与 `scripts/train_sft.sh` 读取的 `data/final/sft_train.jsonl` 一致
- 只是**样例**，不代表真实数据分布，也不能用来训练出可用模型

## 格式

每行一个 JSON：

```json
{
  "id": "7422f7b075a69ed7f8648f06564279db",
  "conversations": [
    {"role": "system", "content": "你是旅行规划助手……"},
    {"role": "user", "content": "西安出发，坐绿皮火车慢游，有啥推荐路线？"},
    {"role": "assistant", "content": "<think>……</think>\n<tool_call>…</tool_call>"},
    {"role": "user", "content": "<tool_response>…</tool_response>"},
    {"role": "assistant", "content": "<answer>……</answer>"}
  ]
}
```

协议要点：

- 工具调用放在 `<tool_call>...</tool_call>` 里
- 最终回答放在 `<answer>...</answer>` 里
- 工具返回以 user 消息里的 `<tool_response>...</tool_response>` 回填

## 脱敏处理

以下内容已替换：

| 类型 | 替换为 |
|---|---|
| 手机号 | `13800000000` |
| 座机 | `010-00000000` |
| 邮箱 | `user@example.com` |
| 本机绝对路径 | `/home/user/project` |
| API Key / Bearer Token | `REDACTED` |

保留了 URL 和工具返回的地点信息，因为这些是训练信号的一部分。第三方网页内容版权归原站点所有。

## 怎么用

先做格式自检（不需要 GPU、不需要 Key）：

```bash
python3 scripts/check_examples.py
```

输出记录数、角色分布、平均长度，并扫描是否还有未脱敏的敏感串。

小规模跑通 SFT（需要 GPU + ms-swift）：

```bash
TRAIN_DATA=examples/sft_sample.jsonl \
VAL_DATA=examples/sft_sample.jsonl \
EXTRA_ARGS="--max_steps 20" \
bash scripts/train_sft.sh
```

这一步只验证“数据能喂进去、训练能启动”，不追求效果。
