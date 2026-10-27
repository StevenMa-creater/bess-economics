"""市场画像：电价分布、负电价、日内 2 小时价差、鸭子曲线。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import DT_H, INTERVALS_PER_DAY


def top_bottom_spread(day_prices: np.ndarray, hours: float = 2.0) -> float:
    """最高 N 个区间均价 − 最低 N 个区间均价（N = hours / 5 分钟）。2h 电池一天一次循环可捕获的价差上限近似。"""
    k = int(round(hours / DT_H))
    s = np.sort(day_prices)
    return float(s[-k:].mean() - s[:k].mean())


def market_profile(df: pd.DataFrame) -> dict:
    p = df.rrp
    days = df.trade_date.nunique()
    out = dict(
        days=days,
        mean=float(p.mean()), median=float(p.median()), std=float(p.std()),
        p05=float(p.quantile(0.05)), p95=float(p.quantile(0.95)),
        negative_share=float((p < 0).mean()),
        negative_hours_per_year=float((p < 0).sum() * DT_H * 365 / days),
        intervals_above_300=int((p > 300).sum()),
        intervals_above_5000=int((p > 5000).sum()),
        min=float(p.min()), max=float(p.max()),
    )
    return out


def daily_spreads(price_matrix: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=price_matrix.index)
    for h in (1, 2, 4):
        out[f"spread_{h}h"] = [top_bottom_spread(r, h) for r in price_matrix.to_numpy()]
    out["daily_mean"] = price_matrix.mean(axis=1)
    out["neg_share"] = (price_matrix < 0).mean(axis=1)
    out["max"] = price_matrix.max(axis=1)
    return out


def monthly_profile(df: pd.DataFrame) -> pd.DataFrame:
    """月 × 一天中的小时 的平均电价（鸭子曲线）。用中位数和截断均值都可，这里用截断均值以免尖峰主导。"""
    d = df.assign(month=df.trade_date.dt.month, hour=df.interval_start.dt.hour + df.interval_start.dt.minute / 60)
    d = d.assign(hour_bin=(d.hour // 0.5) * 0.5, rrp_c=d.rrp.clip(-500, 1000))
    return d.pivot_table(index="hour_bin", columns="month", values="rrp_c", aggfunc="mean")


def naive_arbitrage_estimate(spreads: pd.DataFrame, power_mw: float, hours: float, rte: float) -> dict:
    """粗算：每天一次循环，按 2h 价差 × 放电量，扣效率损失。只是量级参考，精确值见 LP。"""
    s = spreads[f"spread_{int(hours)}h"]
    e = power_mw * hours
    # 放电 e，充电 e/rte：收益 ≈ e × (p_high − p_low / rte)，用 spread 近似并扣掉低价段的效率损失
    rev = (s * e).clip(lower=0).sum() * 365 / len(s) * rte
    return dict(revenue_aud_per_year=float(rev), revenue_aud_per_mw_year=float(rev / power_mw))
