"""澳洲电站经济性：把回测得到的真实可实现收益接到 15 年现金流，用蒙特卡洛给出 IRR 分布。

口径：税前、实际（不含通胀）全投资 IRR。参数都是"假设"，全部写在 AUAssumptions 里，README 列出来源。
不确定性来源：
1. 投资成本：三角分布（GenCost 与近期项目区间）
2. 单年收益：从回测期的逐日收益中有放回抽样 365 天（保留尖峰事件的偶然性）
3. 市场结构：年度收益乘数 ~ 对数正态，并叠加逐年下降趋势（储能装机增加压缩价差，即"自我蚕食"）
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .china_irr.model import irr


@dataclass
class AUAssumptions:
    power_mw: float = 100.0
    duration_h: float = 2.0
    capex_aud_per_kwh: tuple = (420.0, 520.0, 650.0)   # 三角分布 (低, 众数, 高)，2h 系统全口径
    fom_rate_of_capex: float = 0.015                    # 年固定运维 = 投资 × 1.5%（假设）
    life_years: int = 15
    capacity_fade_per_year: float = 0.02
    market_sigma: float = 0.30                          # 年度市场乘数的对数标准差
    cannibalisation_per_year: float = 0.03              # 价差逐年压缩
    other_revenue_aud_per_mw_year: float = 0.0          # FCAS / 容量合同等（本模型不模拟，按情景给定）
    discount_rate: float = 0.07                         # 实际 WACC（假设）
    hurdle: float = 0.08

    @property
    def energy_mwh(self) -> float:
        return self.power_mw * self.duration_h


def simulate(daily_revenue: pd.Series, a: AUAssumptions, n: int = 5000, seed: int = 42) -> pd.DataFrame:
    """daily_revenue：回测中某策略的逐日收益（A$），对应同一块电池。"""
    rng = np.random.default_rng(seed)
    dr = daily_revenue.to_numpy()
    lo, mode, hi = a.capex_aud_per_kwh
    rows = []
    for _ in range(n):
        capex = rng.triangular(lo, mode, hi) * a.energy_mwh * 1e3
        fom = capex * a.fom_rate_of_capex
        cf = [-capex]
        for y in range(1, a.life_years + 1):
            year = rng.choice(dr, size=365, replace=True).sum()
            mult = rng.lognormal(-0.5 * a.market_sigma ** 2, a.market_sigma)
            trend = (1 - a.cannibalisation_per_year) ** (y - 1)
            fade = (1 - a.capacity_fade_per_year) ** (y - 1)
            rev = year * mult * trend * fade + a.other_revenue_aud_per_mw_year * a.power_mw
            cf.append(rev - fom)
        cf = np.array(cf)
        t = np.arange(len(cf))
        rows.append(dict(capex=capex, irr=irr(cf, lo=-0.99, hi=1.0),
                         npv=float(np.sum(cf / (1 + a.discount_rate) ** t)),
                         year1_revenue=cf[1] + fom))
    return pd.DataFrame(rows)


def summarise(sim: pd.DataFrame, a: AUAssumptions) -> dict:
    irr_v = sim.irr.dropna()
    return dict(n=len(sim), irr_p10=float(irr_v.quantile(0.10)) if len(irr_v) else np.nan,
                irr_p50=float(irr_v.quantile(0.50)) if len(irr_v) else np.nan,
                irr_p90=float(irr_v.quantile(0.90)) if len(irr_v) else np.nan,
                irr_undefined_share=float(sim.irr.isna().mean()),
                prob_irr_above_hurdle=float((sim.irr > a.hurdle).mean()),
                npv_p50_aud_m=float(sim.npv.median() / 1e6),
                prob_npv_positive=float((sim.npv > 0).mean()))


def breakeven_revenue_per_mw(a: AUAssumptions, target_irr: float | None = None) -> float:
    """在众数投资成本下，要达到 target_irr 需要的第 1 年收入（A$/MW/年）。"""
    target = a.discount_rate if target_irr is None else target_irr
    capex = a.capex_aud_per_kwh[1] * a.energy_mwh * 1e3
    fom = capex * a.fom_rate_of_capex

    def irr_at(r1: float) -> float:
        cf = [-capex] + [r1 * a.power_mw * (1 - a.cannibalisation_per_year) ** (y - 1)
                         * (1 - a.capacity_fade_per_year) ** (y - 1) - fom for y in range(1, a.life_years + 1)]
        return irr(np.array(cf))

    lo, hi = 1e3, 2e6
    for _ in range(80):
        mid = (lo + hi) / 2
        v = irr_at(mid)
        if np.isnan(v) or v < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
