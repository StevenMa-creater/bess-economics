"""AEMO 电价数据：读取、清洗、校验。

数据来源（两种，任选其一放进 data/raw/）：
1. AEMO "Aggregated price and demand data" 月度文件（推荐，浏览器直接下载）：
   https://aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_{YYYYMM}_{REGION}.csv
   列：REGION, SETTLEMENTDATE, TOTALDEMAND, RRP, PERIODTYPE
2. NEMOSIS 下载的 DISPATCHPRICE 表（见 download_nemosis）。

时间约定：SETTLEMENTDATE 是区间**结束**时刻，NEM 时间 = AEST（UTC+10，不跟夏令时）。
2021-10-01 起 NEM 实行 5 分钟结算（5MS），每天 288 个区间。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

INTERVAL = pd.Timedelta(minutes=5)
INTERVALS_PER_DAY = 288
DT_H = 5 / 60  # 每个区间的小时数

AEMO_URL = "https://aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_{ym}_{region}.csv"


def monthly_urls(region: str, start: str, end: str) -> list[str]:
    """生成 [start, end) 月份的下载链接。start/end 形如 '2024-01'。"""
    months = pd.period_range(start, end, freq="M")[:-1]
    return [AEMO_URL.format(ym=m.strftime("%Y%m"), region=region) for m in months]


def download_nemosis(region: str, start: str, end: str, cache: Path) -> pd.DataFrame:
    """可选：用 NEMOSIS 下载 DISPATCHPRICE（需能访问 nemweb.com.au）。start/end 形如 '2025/01/01 00:00:00'。"""
    from nemosis import dynamic_data_compiler  # 延迟导入，非必需依赖
    cache.mkdir(parents=True, exist_ok=True)
    df = dynamic_data_compiler(start, end, "DISPATCHPRICE", str(cache),
                               filter_cols=["REGIONID"], filter_values=([region],),
                               select_columns=["SETTLEMENTDATE", "REGIONID", "RRP", "INTERVENTION"])
    return df


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """把两种来源统一成 settlementdate / region / rrp / demand 四列。"""
    cols = {c.upper(): c for c in df.columns}
    out = pd.DataFrame()
    out["settlementdate"] = pd.to_datetime(df[cols["SETTLEMENTDATE"]], format="mixed")
    region_col = cols.get("REGION") or cols.get("REGIONID")
    out["region"] = df[region_col].astype(str)
    out["rrp"] = pd.to_numeric(df[cols["RRP"]], errors="coerce")
    out["demand"] = pd.to_numeric(df[cols["TOTALDEMAND"]], errors="coerce") if "TOTALDEMAND" in cols else np.nan
    if "INTERVENTION" in cols:  # DISPATCHPRICE：干预定价时同一时刻有两行，保留市场价（INTERVENTION=0）
        out = out[pd.to_numeric(df[cols["INTERVENTION"]], errors="coerce").fillna(0).to_numpy() == 0]
    return out


def load_raw(files: Iterable[Path], region: str) -> pd.DataFrame:
    frames = [_normalise(pd.read_csv(f)) for f in sorted(files)]
    if not frames:
        raise FileNotFoundError("data/raw/ 里没有找到 CSV。请按 README 下载 AEMO 月度文件。")
    df = pd.concat(frames, ignore_index=True)
    return df[df.region == region]


def clean(df: pd.DataFrame, log: list[str] | None = None) -> pd.DataFrame:
    """排序、去重、加区间开始时刻和交易日。"""
    log = log if log is not None else []
    n0 = len(df)
    df = df.dropna(subset=["rrp"]).sort_values("settlementdate")
    df = df.drop_duplicates(subset=["settlementdate"], keep="last")
    log.append(f"清洗：{n0} 行 → {len(df)} 行（去空值与重复时间戳）")
    df = df.assign(interval_start=df.settlementdate - INTERVAL)
    df = df.assign(trade_date=df.interval_start.dt.normalize(),
                   slot=((df.interval_start - df.interval_start.dt.normalize()) // INTERVAL).astype(int))
    return df.reset_index(drop=True)


def complete_days(df: pd.DataFrame, log: list[str] | None = None) -> pd.DataFrame:
    """只保留 288 个区间齐全的交易日（个别缺口按前值补齐，缺口 > 6 个区间的日子剔除）。"""
    log = log if log is not None else []
    full = []
    dropped = 0
    filled = 0
    for d, g in df.groupby("trade_date"):
        g = g.set_index("slot").reindex(range(INTERVALS_PER_DAY))
        missing = g.rrp.isna().sum()
        if missing > 6:
            dropped += 1
            continue
        if missing:
            filled += int(missing)
            g[["rrp", "demand"]] = g[["rrp", "demand"]].ffill().bfill()
            g["trade_date"] = d
            g["interval_start"] = d + g.index.to_series() * INTERVAL
            g["settlementdate"] = g["interval_start"] + INTERVAL
            g["region"] = g["region"].ffill().bfill()
        full.append(g.reset_index())
    out = pd.concat(full, ignore_index=True)
    log.append(f"完整性：保留 {out.trade_date.nunique()} 个交易日，剔除 {dropped} 天，补齐 {filled} 个缺失区间")
    return out


def validate(df: pd.DataFrame) -> dict:
    """返回校验摘要；严重问题直接报错。"""
    if df.rrp.isna().any():
        raise ValueError("RRP 存在空值")
    per_day = df.groupby("trade_date").size()
    if not (per_day == INTERVALS_PER_DAY).all():
        raise ValueError("存在区间数不是 288 的交易日")
    return dict(days=int(per_day.size), rows=int(len(df)),
                first=str(df.trade_date.min().date()), last=str(df.trade_date.max().date()),
                rrp_min=float(df.rrp.min()), rrp_max=float(df.rrp.max()))


def load(raw_dir: Path, region: str = "QLD1", cache_file: Path | None = None, log: list[str] | None = None) -> pd.DataFrame:
    """一站式：读 raw 目录下所有 CSV → 清洗 → 补齐 → 校验，可选缓存为 parquet。"""
    if cache_file is not None and cache_file.exists():
        return pd.read_parquet(cache_file)
    files = list(Path(raw_dir).glob("*.csv")) + list(Path(raw_dir).glob("*.CSV"))
    df = complete_days(clean(load_raw(files, region), log), log)
    validate(df)
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_file)
    return df


def to_matrix(df: pd.DataFrame, col: str = "rrp") -> pd.DataFrame:
    """转成 交易日 × 288 区间 的矩阵，方便逐日优化。"""
    return df.pivot(index="trade_date", columns="slot", values=col).sort_index()
