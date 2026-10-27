"""储能逐日最优调度（线性规划 / 混合整数规划，scipy + HiGHS）。

决策变量（每个 5 分钟区间 t）：
    c_t  充电功率 MW    0 ≤ c_t ≤ P
    d_t  放电功率 MW    0 ≤ d_t ≤ P
    s_t  区间末储能量 MWh   E_min ≤ s_t ≤ E_max
约束：
    s_t = s_{t-1} + η·c_t·Δt − d_t·Δt / η          （η = √RTE，充放电各承担一半损耗）
    s_T = s_0                                        （每日能量中性，日与日之间解耦）
    Σ d_t·Δt ≤ k · E_usable                          （可选：每日最多 k 次等效满循环，质保常见约束）
    c_t ≤ P·u_t,  d_t ≤ P·(1 − u_t),  u_t ∈ {0,1}    （可选：同一区间不能同时充放；
                                                       不加时负电价下 LP 会利用往返损耗"空耗"电量套利）
目标：
    max Σ p_t·(d_t − c_t)·Δt  −  δ · Σ d_t·Δt         （δ = 单位放电量的衰减成本，A$/MWh）
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, linprog, milp

from .nem.data import DT_H, INTERVALS_PER_DAY


@dataclass(frozen=True)
class Battery:
    power_mw: float = 100.0
    energy_mwh: float = 200.0
    rte: float = 0.86               # 交流侧往返效率
    soc_min: float = 0.05
    soc_max: float = 0.95
    soc_init: float = 0.50
    max_cycles_per_day: Optional[float] = 1.5
    degradation_cost: float = 0.0   # A$/MWh 放电量
    exclusive: bool = True          # 禁止同时充放（MILP）

    @property
    def eta(self) -> float:
        return float(np.sqrt(self.rte))

    @property
    def usable_mwh(self) -> float:
        return self.energy_mwh * (self.soc_max - self.soc_min)

    def with_(self, **kw) -> "Battery":
        return replace(self, **kw)


@dataclass
class DayResult:
    revenue: float          # A$，按 price_actual 结算
    charge_mwh: float
    discharge_mwh: float
    charge: np.ndarray      # MW
    discharge: np.ndarray   # MW
    soc: np.ndarray         # MWh


def _build(prices: np.ndarray, b: Battery):
    T = len(prices)
    dt = DT_H
    eta = b.eta
    n = 3 * T if not b.exclusive else 4 * T
    ic, id_, is_ = 0, T, 2 * T

    # 目标（最小化）：−收入 + 衰减
    cost = np.zeros(n)
    cost[ic:ic + T] = prices * dt
    cost[id_:id_ + T] = -prices * dt + b.degradation_cost * dt

    # 储能平衡：s_t − s_{t−1} − η·dt·c_t + dt/η·d_t = 0
    rows, cols, vals = [], [], []
    for t in range(T):
        rows += [t, t, t]
        cols += [is_ + t, ic + t, id_ + t]
        vals += [1.0, -eta * dt, dt / eta]
        if t > 0:
            rows.append(t); cols.append(is_ + t - 1); vals.append(-1.0)
    A_eq = sparse.csr_matrix((vals, (rows, cols)), shape=(T, n))
    b_eq = np.zeros(T)
    s0 = b.soc_init * b.energy_mwh
    b_eq[0] = s0

    lb = np.zeros(n)
    ub = np.zeros(n)
    ub[ic:ic + T] = b.power_mw
    ub[id_:id_ + T] = b.power_mw
    lb[is_:is_ + T] = b.soc_min * b.energy_mwh
    ub[is_:is_ + T] = b.soc_max * b.energy_mwh
    lb[is_ + T - 1] = ub[is_ + T - 1] = s0   # 日末回到初始 SOC

    A_ub_rows, b_ub = [], []
    if b.max_cycles_per_day is not None:
        r = np.zeros(n); r[id_:id_ + T] = dt
        A_ub_rows.append(r); b_ub.append(b.max_cycles_per_day * b.usable_mwh)
    if b.exclusive:
        iu = 3 * T
        ub[iu:iu + T] = 1.0
        # c_t − P·u_t ≤ 0 ;  d_t + P·u_t ≤ P
        rr, cc, vv = [], [], []
        for t in range(T):
            rr += [t, t]; cc += [ic + t, iu + t]; vv += [1.0, -b.power_mw]
            rr += [T + t, T + t]; cc += [id_ + t, iu + t]; vv += [1.0, b.power_mw]
        A_ex = sparse.csr_matrix((vv, (rr, cc)), shape=(2 * T, n))
        b_ex = np.r_[np.zeros(T), np.full(T, b.power_mw)]
    else:
        A_ex = b_ex = None
    A_ub = sparse.csr_matrix(np.array(A_ub_rows)) if A_ub_rows else None
    return T, n, cost, A_eq, b_eq, A_ub, (np.array(b_ub) if b_ub else None), A_ex, b_ex, lb, ub


def optimise_day(price_plan: np.ndarray, b: Battery, price_actual: Optional[np.ndarray] = None) -> DayResult:
    """按 price_plan 做计划，按 price_actual 结算（不给则与 plan 相同 = 完美预知）。"""
    price_plan = np.asarray(price_plan, dtype=float)
    price_actual = price_plan if price_actual is None else np.asarray(price_actual, dtype=float)
    T, n, cost, A_eq, b_eq, A_ub, b_ub, A_ex, b_ex, lb, ub = _build(price_plan, b)

    if b.exclusive:
        cons = [LinearConstraint(A_eq, b_eq, b_eq)]
        if A_ub is not None:
            cons.append(LinearConstraint(A_ub, -np.inf, b_ub))
        cons.append(LinearConstraint(A_ex, -np.inf, b_ex))
        integrality = np.zeros(n); integrality[3 * T:] = 1
        res = milp(cost, constraints=cons, integrality=integrality, bounds=Bounds(lb, ub),
                   options={"mip_rel_gap": 1e-4, "time_limit": 30})
    else:
        res = linprog(cost, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                      bounds=list(zip(lb, ub)), method="highs")
    if res.x is None:
        raise RuntimeError(f"优化失败：{res.message}")
    x = res.x
    c = np.clip(x[:T], 0, None)
    d = np.clip(x[T:2 * T], 0, None)
    s = x[2 * T:3 * T]
    revenue = float(np.sum(price_actual * (d - c)) * DT_H)
    return DayResult(revenue=revenue, charge_mwh=float(c.sum() * DT_H), discharge_mwh=float(d.sum() * DT_H),
                     charge=c, discharge=d, soc=s)


def fixed_schedule_day(price_actual: np.ndarray, b: Battery, charge_hours=(10, 12), discharge_hours=(17, 19)) -> DayResult:
    """规则策略：每天固定时段满功率充、固定时段满功率放（对照组，不用任何预测）。"""
    T = len(price_actual)
    c = np.zeros(T); d = np.zeros(T)
    per_h = int(round(1 / DT_H))
    c[charge_hours[0] * per_h:charge_hours[1] * per_h] = b.power_mw
    # 放电量按充电量 × RTE，且不超过可用容量
    e_in = min(c.sum() * DT_H * b.eta, b.usable_mwh)
    c *= (e_in / b.eta) / max(c.sum() * DT_H, 1e-9)
    e_out = e_in * b.eta
    n_dis = discharge_hours[1] * per_h - discharge_hours[0] * per_h
    d[discharge_hours[0] * per_h:discharge_hours[1] * per_h] = min(b.power_mw, e_out / (n_dis * DT_H))
    revenue = float(np.sum(price_actual * (d - c)) * DT_H)
    return DayResult(revenue=revenue, charge_mwh=float(c.sum() * DT_H), discharge_mwh=float(d.sum() * DT_H),
                     charge=c, discharge=d, soc=np.full(T, np.nan))


def run_year(price_actual: pd.DataFrame, b: Battery, price_plan: Optional[pd.DataFrame] = None,
             strategy: str = "lp") -> pd.DataFrame:
    """逐日运行。price_* 为 交易日 × 288 矩阵。返回每日收益表。"""
    rows = []
    for day, actual in price_actual.iterrows():
        a = actual.to_numpy()
        if strategy == "fixed":
            r = fixed_schedule_day(a, b)
        else:
            plan = a if price_plan is None else price_plan.loc[day].to_numpy()
            r = optimise_day(plan, b, a)
        rows.append(dict(trade_date=day, revenue=r.revenue, charge_mwh=r.charge_mwh, discharge_mwh=r.discharge_mwh))
    out = pd.DataFrame(rows).set_index("trade_date")
    out["cycles"] = out.discharge_mwh / b.usable_mwh
    return out


def summarise(daily: pd.DataFrame, b: Battery) -> dict:
    days = len(daily)
    scale = 365 / days
    rev = daily.revenue.sum() * scale
    return dict(days=days, revenue_aud_per_year=rev, revenue_aud_per_mw_year=rev / b.power_mw,
                cycles_per_year=daily.cycles.sum() * scale,
                avg_spread_captured=(daily.revenue.sum() / daily.discharge_mwh.sum()) if daily.discharge_mwh.sum() else np.nan)
