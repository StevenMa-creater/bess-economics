"""独立储能电站全投资 / 资本金 IRR 模型（年度现金流）。

设计目标：
- 把 2026 年国内储能电站的四类收益（电能量套利、容量电价、辅助服务、容量租赁）
  和成本（初始投资、运维、电池增容、税、贷款）放进同一张年度现金流表；
- 所有参数显式可调，便于做敏感性分析；
- 不依赖 Excel，可在命令行一键复算。

单位约定：
- 功率 MW，容量 MWh，电量 MWh；
- 电价 元/kWh；容量类价格 元/kW·年；
- 金额 万元（1 万元 = 1e4 元），输出表统一为万元。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, replace
from typing import Optional

import numpy as np
import pandas as pd

WAN = 1e4  # 元 -> 万元


@dataclass
class Assumptions:
    """一个电站方案的全部输入。默认值对应"通用 2h 独立储能电站，2026 年价格"。"""

    name: str = "通用方案"
    # ---- 规模 ----
    power_mw: float = 100.0
    duration_h: float = 2.0
    project_years: int = 15
    # ---- 投资（元/Wh，按额定容量 MWh 计）----
    capex_system_yuan_per_wh: float = 0.60   # 储能系统（电池+PCS+BMS+EMS+集装箱），2026H1 2h 中标均价 0.5993 元/Wh
    capex_bop_yuan_per_wh: float = 0.30      # 土建、升压站、并网、开发费、设计监理等
    capex_other_rate: float = 0.03           # 建设期利息、预备费等，按（系统+BOP）比例
    # ---- 运行参数 ----
    cycles_per_day: float = 1.2              # 等效满充满放次数/天
    rte: float = 0.86                        # 交流侧往返效率
    dod: float = 0.95                        # 放电深度
    availability: float = 0.96               # 可用率（含检修与故障）
    degradation_per_year: float = 0.025      # 可用容量年衰减
    augmentation_year: Optional[int] = 8     # 第几年增容补电（None 表示不增容）
    augmentation_cost_yuan_per_wh: float = 0.45
    # ---- 收益：电能量 ----
    charge_price_yuan_per_kwh: float = 0.25  # 平均充电电价（现货谷段 / 负电价时段）
    discharge_price_yuan_per_kwh: float = 0.70  # 平均放电电价（现货峰段）
    # ---- 收益：容量类 ----
    coal_capacity_price_yuan_per_kw_yr: float = 165.0   # 本省煤电容量电价基准
    net_load_peak_hours: float = 4.0                     # 本省净负荷高峰时长（114 号文公式分母）
    capacity_price_override: Optional[float] = None      # 直接给定储能容量电价（元/kW·年），覆盖公式
    capacity_price_compliance: float = 0.90              # 考核折扣（不达标扣减）
    capacity_lease_yuan_per_kw_yr: float = 0.0           # 容量租赁（山东存量模式常见，新增项目默认 0）
    ancillary_yuan_per_kw_yr: float = 20.0               # 调频/备用等辅助服务净收益
    # ---- 成本 ----
    opex_rate_of_capex: float = 0.015        # 年运维费占初始投资比例
    insurance_rate_of_capex: float = 0.003
    land_lease_wan_per_year: float = 60.0
    # ---- 财务 ----
    debt_ratio: float = 0.70
    loan_rate: float = 0.035
    loan_years: int = 12
    depreciation_years: int = 15
    residual_value_rate: float = 0.05
    income_tax_rate: float = 0.25
    tax_holiday_3_3: bool = True             # 三免三减半（储能是否适用各地不一，未核实）
    discount_rate: float = 0.08              # NPV 折现率 / 行业常用门槛

    # ---- 派生量 ----
    @property
    def energy_mwh(self) -> float:
        return self.power_mw * self.duration_h

    @property
    def capex_wan(self) -> float:
        base = self.energy_mwh * 1e3 * (self.capex_system_yuan_per_wh + self.capex_bop_yuan_per_wh) * 1e3 / WAN
        # energy_mwh*1e3 = kWh; *1e3 = Wh
        return base * (1 + self.capex_other_rate)

    @property
    def capacity_price_yuan_per_kw_yr(self) -> float:
        """114 号文：储能容量电价 = 煤电容量电价 × min(1, 放电时长 / 净负荷高峰时长)。"""
        if self.capacity_price_override is not None:
            return self.capacity_price_override
        ratio = min(1.0, self.duration_h / self.net_load_peak_hours)
        return self.coal_capacity_price_yuan_per_kw_yr * ratio

    def with_(self, **kw) -> "Assumptions":
        return replace(self, **kw)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["energy_mwh"] = self.energy_mwh
        d["capex_wan"] = round(self.capex_wan, 1)
        d["capacity_price_yuan_per_kw_yr"] = round(self.capacity_price_yuan_per_kw_yr, 1)
        return d


# ----------------------------------------------------------------------------
# 财务工具函数
# ----------------------------------------------------------------------------

def npv(rate: float, cashflows: np.ndarray) -> float:
    t = np.arange(len(cashflows))
    return float(np.sum(cashflows / (1 + rate) ** t))


def irr(cashflows: np.ndarray, lo: float = -0.99, hi: float = 1.0, tol: float = 1e-7) -> float:
    """二分法求 IRR。现金流第 0 期为投资（负）。无解返回 nan。"""
    cf = np.asarray(cashflows, dtype=float)
    f_lo, f_hi = npv(lo, cf), npv(hi, cf)
    if np.isnan(f_lo) or np.isnan(f_hi) or f_lo * f_hi > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid, cf)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def payback_years(cashflows: np.ndarray) -> float:
    """静态回收期（年，线性插值）。无法回收返回 nan。"""
    cum = np.cumsum(cashflows)
    for i in range(1, len(cum)):
        if cum[i] >= 0:
            prev = cum[i - 1]
            return (i - 1) + (-prev / cashflows[i] if cashflows[i] != 0 else 0)
    return float("nan")


def annuity_payment(principal: float, rate: float, years: int) -> float:
    if rate == 0:
        return principal / years
    return principal * rate / (1 - (1 + rate) ** (-years))


# ----------------------------------------------------------------------------
# 主模型
# ----------------------------------------------------------------------------

def build_cashflow(a: Assumptions) -> pd.DataFrame:
    """返回逐年现金流表（万元）。第 0 年为建设期。"""
    years = np.arange(0, a.project_years + 1)
    rows = []

    capex = a.capex_wan
    debt = capex * a.debt_ratio
    equity = capex - debt
    annual_debt_service = annuity_payment(debt, a.loan_rate, a.loan_years)
    depreciation = capex * (1 - a.residual_value_rate) / a.depreciation_years

    soh = 1.0
    loan_balance = debt
    kw = a.power_mw * 1e3

    for y in years:
        if y == 0:
            rows.append(dict(year=0, soh=1.0, discharged_mwh=0, rev_energy=0, rev_capacity=0,
                             rev_lease=0, rev_ancillary=0, revenue=0, opex=0, augmentation=0,
                             ebitda=0, depreciation=0, interest=0, principal=0, tax=0,
                             cf_project_pretax=-capex, cf_project_posttax=-capex, cf_equity=-equity))
            continue

        # ---- 运行 ----
        if a.augmentation_year is not None and y == a.augmentation_year:
            aug_cost = (1 - soh) * a.energy_mwh * 1e6 * a.augmentation_cost_yuan_per_wh / WAN
            soh = 1.0
        else:
            aug_cost = 0.0

        discharged_mwh = (a.energy_mwh * a.dod * soh * a.cycles_per_day * 365 * a.availability)
        charged_mwh = discharged_mwh / a.rte

        rev_energy = (discharged_mwh * 1e3 * a.discharge_price_yuan_per_kwh
                      - charged_mwh * 1e3 * a.charge_price_yuan_per_kwh) / WAN
        rev_capacity = kw * a.capacity_price_yuan_per_kw_yr * a.capacity_price_compliance / WAN
        rev_lease = kw * a.capacity_lease_yuan_per_kw_yr / WAN
        rev_ancillary = kw * a.ancillary_yuan_per_kw_yr / WAN
        revenue = rev_energy + rev_capacity + rev_lease + rev_ancillary

        opex = capex * (a.opex_rate_of_capex + a.insurance_rate_of_capex) + a.land_lease_wan_per_year
        ebitda = revenue - opex

        # ---- 贷款 ----
        if y <= a.loan_years and loan_balance > 1e-9:
            interest = loan_balance * a.loan_rate
            principal = min(annual_debt_service - interest, loan_balance)
            loan_balance -= principal
        else:
            interest = principal = 0.0

        # ---- 税 ----
        dep = depreciation if y <= a.depreciation_years else 0.0
        taxable = ebitda - dep - interest - aug_cost
        rate = a.income_tax_rate
        if a.tax_holiday_3_3:
            if y <= 3:
                rate = 0.0
            elif y <= 6:
                rate = a.income_tax_rate / 2
        tax = max(0.0, taxable) * rate

        # 项目税前 / 税后 / 资本金现金流
        cf_project_pretax = ebitda - aug_cost
        cf_project_posttax = ebitda - aug_cost - tax
        cf_equity = ebitda - aug_cost - tax - interest - principal
        if y == a.project_years:
            residual = capex * a.residual_value_rate
            cf_project_pretax += residual
            cf_project_posttax += residual
            cf_equity += residual

        soh_used = soh
        soh *= (1 - a.degradation_per_year)
        rows.append(dict(year=y, soh=round(soh_used, 4), discharged_mwh=discharged_mwh,
                         rev_energy=rev_energy, rev_capacity=rev_capacity, rev_lease=rev_lease,
                         rev_ancillary=rev_ancillary, revenue=revenue, opex=opex, augmentation=aug_cost,
                         ebitda=ebitda, depreciation=dep, interest=interest, principal=principal, tax=tax,
                         cf_project_pretax=cf_project_pretax, cf_project_posttax=cf_project_posttax,
                         cf_equity=cf_equity))
    return pd.DataFrame(rows)


@dataclass
class Result:
    name: str
    capex_wan: float
    capacity_price: float
    revenue_year1_wan: float
    irr_project_pretax: float
    irr_project_posttax: float
    irr_equity: float
    npv_posttax_wan: float
    payback_project_years: float
    lcos_yuan_per_kwh: float
    revenue_mix_year1: dict = field(default_factory=dict)
    breakeven_revenue_wan: Optional[float] = None

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("revenue_mix_year1")
        return d


def evaluate(a: Assumptions, hurdle: Optional[float] = None) -> Result:
    df = build_cashflow(a)
    cf_pre = df.cf_project_pretax.to_numpy()
    cf_post = df.cf_project_posttax.to_numpy()
    cf_eq = df.cf_equity.to_numpy()

    # LCOS：全生命周期成本现值 / 放电量现值
    t = df.year.to_numpy()
    disc = (1 + a.discount_rate) ** t
    costs = np.where(t == 0, a.capex_wan, df.opex + df.augmentation) * WAN
    lcos = float(np.sum(costs / disc) / np.sum(df.discharged_mwh.to_numpy() * 1e3 / disc))

    y1 = df.iloc[1]
    mix = {k: round(float(y1[k]) / float(y1.revenue), 3) if y1.revenue else 0.0
           for k in ("rev_energy", "rev_capacity", "rev_lease", "rev_ancillary")}

    res = Result(
        name=a.name,
        capex_wan=round(a.capex_wan, 1),
        capacity_price=round(a.capacity_price_yuan_per_kw_yr, 1),
        revenue_year1_wan=round(float(y1.revenue), 1),
        irr_project_pretax=irr(cf_pre),
        irr_project_posttax=irr(cf_post),
        irr_equity=irr(cf_eq),
        npv_posttax_wan=round(npv(a.discount_rate, cf_post), 1),
        payback_project_years=payback_years(cf_pre),
        lcos_yuan_per_kwh=round(lcos, 3),
        revenue_mix_year1=mix,
    )
    if hurdle is not None:
        res.breakeven_revenue_wan = breakeven_revenue(a, hurdle)
    return res


def breakeven_revenue(a: Assumptions, hurdle: float = 0.06) -> float:
    """使全投资税后 IRR 恰好等于 hurdle 时，第 1 年所需的总收入（万元）。

    做法：对所有收益价格同比例缩放 k，二分求 k。
    """
    def irr_at(k: float) -> float:
        b = a.with_(charge_price_yuan_per_kwh=a.charge_price_yuan_per_kwh,  # 充电成本不缩放
                    discharge_price_yuan_per_kwh=a.charge_price_yuan_per_kwh / a.rte
                    + (a.discharge_price_yuan_per_kwh - a.charge_price_yuan_per_kwh / a.rte) * k,
                    capacity_price_override=a.capacity_price_yuan_per_kw_yr * k,
                    capacity_lease_yuan_per_kw_yr=a.capacity_lease_yuan_per_kw_yr * k,
                    ancillary_yuan_per_kw_yr=a.ancillary_yuan_per_kw_yr * k)
        return evaluate(b).irr_project_posttax

    lo, hi = 0.1, 6.0
    for _ in range(80):
        mid = (lo + hi) / 2
        v = irr_at(mid)
        if np.isnan(v) or v < hurdle:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    b = a.with_(discharge_price_yuan_per_kwh=a.charge_price_yuan_per_kwh / a.rte
                + (a.discharge_price_yuan_per_kwh - a.charge_price_yuan_per_kwh / a.rte) * k,
                capacity_price_override=a.capacity_price_yuan_per_kw_yr * k,
                capacity_lease_yuan_per_kw_yr=a.capacity_lease_yuan_per_kw_yr * k,
                ancillary_yuan_per_kw_yr=a.ancillary_yuan_per_kw_yr * k)
    return round(float(build_cashflow(b).iloc[1].revenue), 1)


def lifetime_revenue_delta(a: Assumptions, **change) -> float:
    """某个参数改变后，全生命周期（名义）总收入变化（万元）。用于"效率 +1% 值多少钱"这类问题。"""
    base = build_cashflow(a).revenue.sum()
    new = build_cashflow(a.with_(**change)).revenue.sum()
    return float(new - base)
