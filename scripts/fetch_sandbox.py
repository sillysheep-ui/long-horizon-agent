"""下载 ChinaTravel 沙盒数据（CC-BY-4.0），转换成 JSONL 供离线评测使用。

数据来源：https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel-Sandbox
（ModelScope 镜像：https://modelscope.cn/datasets/Cbphcr/ChinaTravel-Sandbox）

用法：
    python3 scripts/fetch_sandbox.py --out data/sandbox
"""

import argparse
import datetime
import json
import sys
import urllib.request
from pathlib import Path

TABLES = ("attractions", "restaurants", "accommodations", "poi", "trains", "flights")
MIRRORS = (
    "https://modelscope.cn/datasets/Cbphcr/ChinaTravel-Sandbox/resolve/master/data/zh/{name}.parquet",
    "https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel-Sandbox/resolve/main/data/zh/{name}.parquet",
)


def _download(name: str, target: Path) -> None:
    for pattern in MIRRORS:
        url = pattern.format(name=name)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                target.write_bytes(response.read())
            print("下载 %s <- %s" % (name, url.split("/resolve/")[0]))
            return
        except Exception as exc:  # noqa: BLE001 - 镜像依次尝试
            print("镜像失败 %s：%s" % (url, exc), file=sys.stderr)
    raise RuntimeError("所有镜像都下载失败：%s" % name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/sandbox")
    parser.add_argument("--tables", default=",".join(TABLES))
    args = parser.parse_args()

    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - 本地开发环境才需要
        raise SystemExit("需要 pandas 与 pyarrow 来转换 parquet：pip install pandas pyarrow") from None

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in [item.strip() for item in args.tables.split(",") if item.strip()]:
        parquet = out / (name + ".parquet")
        _download(name, parquet)
        frame = pd.read_parquet(parquet)
        jsonl = out / (name + ".jsonl")
        with jsonl.open("w", encoding="utf-8") as handle:
            for record in frame.to_dict(orient="records"):
                clean = {key: (None if value != value else value) for key, value in record.items()}
                handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
        parquet.unlink()
        print("写入 %s（%d 条）" % (jsonl, len(frame)))

    meta = out / "META.json"
    payload = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    payload.update({
        "source": "LAMDA-NeSy/ChinaTravel-Sandbox (CC-BY-4.0)",
        "downloaded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tables": sorted(item.name for item in out.glob("*.jsonl")),
    })
    meta.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("写入 %s" % meta)


if __name__ == "__main__":
    main()
