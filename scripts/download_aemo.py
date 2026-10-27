"""在你自己的电脑上运行：下载 AEMO 月度电价文件到 data/raw/。

    python scripts/download_aemo.py                 # 默认 QLD1，2024-01 到 2025-12，共 24 个文件
    python scripts/download_aemo.py --region NSW1 --start 2025-01 --end 2026-01

只用 Python 标准库，Windows / macOS 都能跑。已下载的文件会跳过。
"""
from __future__ import annotations

import argparse
import time
import urllib.request
from pathlib import Path

URL = "https://aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_{ym}_{region}.csv"


def months(start: str, end: str):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) < (ey, em):
        yield f"{y}{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="QLD1")
    ap.add_argument("--start", default="2024-01")
    ap.add_argument("--end", default="2026-01", help="不含该月")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "raw"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for ym in months(a.start, a.end):
        f = out / f"PRICE_AND_DEMAND_{ym}_{a.region}.csv"
        if f.exists() and f.stat().st_size > 10_000:
            print(f"跳过（已存在）{f.name}")
            ok += 1
            continue
        req = urllib.request.Request(URL.format(ym=ym, region=a.region),
                                     headers={"User-Agent": "Mozilla/5.0 (bess-economics research)"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    f.write_bytes(r.read())
                print(f"已下载 {f.name}（{f.stat().st_size/1e3:.0f} KB）")
                ok += 1
                break
            except Exception as e:  # noqa: BLE001
                print(f"失败 {f.name}：{e}（第 {attempt + 1} 次）")
                time.sleep(2)
    print(f"\n完成：{ok} 个文件在 {out}")


if __name__ == "__main__":
    main()
