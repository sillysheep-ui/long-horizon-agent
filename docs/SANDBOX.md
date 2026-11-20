# 离线沙盒（ChinaTravel）

评测默认走真实联网（高德 + 网页搜索），结果会受网络、限流和实时数据影响。
为了让分数可复现，我们接入了 [ChinaTravel](https://www.lamda.nju.edu.cn/shaojj/ChinaTravel/index.html)
的公开沙盒数据（[HuggingFace](https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel-Sandbox) /
[ModelScope 镜像](https://modelscope.cn/datasets/Cbphcr/ChinaTravel-Sandbox)）。

## 数据

- 许可：CC-BY-4.0（可商用、可修改，需署名 LAMDA-NeSy / ChinaTravel）。
- 覆盖 10 个城市：北京、上海、广州、深圳、成都、重庆、杭州、苏州、武汉、南京。
- 表：`attractions` 3,413、`restaurants` 4,655、`accommodations` 3,866、`poi` 12,172、
  `trains` 5,345、`flights` 720、`subway_stations` 3,635。
- 字段里带经纬度、开放/闭馆时间、门票/人均价格、菜系与推荐菜，可用于营业时间、
  预算和空间距离检查。

## 下载

```bash
# 需要 pandas + pyarrow（只在本机转换用，运行时不依赖）
python3 scripts/fetch_sandbox.py --out data/sandbox
```

脚本会依次尝试 ModelScope 与 HuggingFace，并把 parquet 转成 JSONL（约 6MB）。
`data/sandbox/` 已在 `.gitignore` 中，不进仓库。

## 用法

```bash
POI_PROVIDER=sandbox MODEL_PROFILE=api_only \
python3 -m evaluation.product_eval --split test --ids <10城用例> --out results/product_eval/sandbox
```

`POI_PROVIDER=sandbox` 时，产品侧 `poi` 查询改为查本地沙盒，不再请求高德或网页搜索；
沙盒里查不到就返回空结果（不自动联网），保证同一份代码、同一份数据得到同一结果。

## 局限

- 只有 10 个城市，其他目的地仍需联网；
- 是快照数据，价格与时刻会过期，报告里应标注沙盒版本；
- 只有结构化字段，没有景点介绍文本，描述性内容仍由模型生成。
