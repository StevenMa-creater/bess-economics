"""生成与 AEMO PRICE_AND_DEMAND 月度文件**格式完全一致**的合成数据，仅用于测试和演示流程。

合成数据不是真实电价，任何结论都不能来自它。真实结果请按 README 下载 AEMO 数据后运行。
形态参考昆州：午间光伏导致低价/负价，傍晚峰值，偶发价格尖峰，春季负价更多。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def synth(region: str, start: str, end: str, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t_end = pd.date_range(pd.Timestamp(start) + pd.Timedelta(minutes=5), pd.Timestamp(end), freq="5min")
    t_start = t_end - pd.Timedelta(minutes=5)
    h = (t_start.hour + t_start.minute / 60).to_numpy()
    doy = t_start.dayofyear.to_numpy()
    season = np.cos(2 * np.pi * (doy - 15) / 365)          # 1 = 夏季（1 月），−1 = 冬季
    spring = np.exp(-((doy - 290) / 40) ** 2)               # 10 月附近负价最多

    base = 85 + 20 * season
    solar = (np.clip(np.sin(np.pi * (h - 6.5) / 11.5), 0, None) ** 1.5) * (110 + 60 * spring)
    evening = 140 * np.exp(-((h - 18.6) / 1.4) ** 2) * (1 + 0.4 * season)
    morning = 40 * np.exp(-((h - 7.2) / 1.0) ** 2)
    # AR(1) 噪声
    n = len(h)
    eps = rng.normal(0, 18, n)
    ar = np.empty(n); ar[0] = 0
    for i in range(1, n):
        ar[i] = 0.97 * ar[i - 1] + eps[i]
    price = base - solar + evening + morning + ar
    # 尖峰：约 0.15% 的区间，偏向傍晚
    spike_p = 0.0015 * (1 + 3 * np.exp(-((h - 18.5) / 1.5) ** 2))
    spikes = rng.random(n) < spike_p
    price[spikes] += rng.lognormal(7.0, 1.0, spikes.sum())
    price = np.clip(price, -1000, 17500)

    demand = 6200 + 900 * season - 1300 * np.clip(np.sin(np.pi * (h - 6.5) / 11.5), 0, None) \
        + 1400 * np.exp(-((h - 18.5) / 2.0) ** 2) + rng.normal(0, 60, n)
    return pd.DataFrame({"REGION": region, "SETTLEMENTDATE": t_end.strftime("%Y/%m/%d %H:%M:%S"),
                         "TOTALDEMAND": demand.round(2), "RRP": price.round(2), "PERIODTYPE": "TRADE"})


def write_monthly(df: pd.DataFrame, out: Path, region: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    ts = pd.to_datetime(df.SETTLEMENTDATE) - pd.Timedelta(minutes=5)
    paths = []
    for ym, g in df.groupby(ts.dt.strftime("%Y%m")):
        p = out / f"PRICE_AND_DEMAND_{ym}_{region}.csv"
        g.to_csv(p, index=False)
        paths.append(p)
    return paths


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/fixture")
    ap.add_argument("--region", default="QLD1")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-01-01")
    a = ap.parse_args()
    ps = write_monthly(synth(a.region, a.start, a.end), Path(a.out), a.region)
    print(f"已生成 {len(ps)} 个合成月度文件 → {a.out}")
