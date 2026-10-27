"""回测：同一块电池、同一年真实电价，比较不同策略能拿到理论上限的多少。

策略：
- perfect      完美预知 MILP（理论上限，任何真实策略都达不到）
- gbm          梯度提升树预测次日电价 → 按预测做 MILP 计划 → 按真实电价结算
- persistence  用昨天的电价当今天的预测 → MILP 计划 → 真实结算
- fixed        固定时段：10–12 点满功率充、17–19 点满功率放，不用任何预测

收益捕获率 = 策略收益 / 完美预知收益。
"""
from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd

from . import forecast as F
from .dispatch import Battery, run_year, summarise

STRATEGY_NAMES = {"perfect": "完美预知（上限）", "gbm": "GBM 预测 + 优化", "persistence": "昨日电价 + 优化", "fixed": "固定时段规则"}


def run_backtest(price: pd.DataFrame, demand: pd.DataFrame | None, battery: Battery,
                 test_start: pd.Timestamp, log=print) -> dict:
    log(f"[预测] 按月滚动训练 GBM，测试期从 {test_start.date()} 开始 …")
    pred = F.walk_forward_gbm(price, demand, test_start)
    test_days = pred.index
    actual = price.loc[test_days]
    pers = F.persistence(price).loc[test_days]
    metrics = {"gbm": F.error_metrics(pred, price), "persistence": F.error_metrics(pers, price)}

    daily = {}
    log("[调度] 完美预知 …"); daily["perfect"] = run_year(actual, battery)
    log("[调度] GBM 预测 …"); daily["gbm"] = run_year(actual, battery, price_plan=pred)
    log("[调度] 昨日电价 …"); daily["persistence"] = run_year(actual, battery, price_plan=pers)
    log("[调度] 固定时段 …"); daily["fixed"] = run_year(actual, battery, strategy="fixed")

    rows = []
    perfect = daily["perfect"].revenue.sum()
    for k, d in daily.items():
        s = summarise(d, battery)
        s.update(strategy=k, name=STRATEGY_NAMES[k], capture_rate=d.revenue.sum() / perfect)
        rows.append(s)
    table = pd.DataFrame(rows).set_index("strategy")
    return dict(pred=pred, actual=actual, persistence=pers, daily=daily, table=table, forecast_metrics=metrics)


def degradation_sweep(actual: pd.DataFrame, battery: Battery, costs=(0, 10, 25, 50, 100),
                      true_cost: float = 30.0, log=print) -> pd.DataFrame:
    """不同"调度时计入的衰减成本 δ"下，年收入与年循环次数的权衡（完美预知）。

    true_cost：按电芯更换成本 / 循环寿命估算的真实衰减成本（A$/MWh 放电量），用来算净收益。
    """
    rows = []
    for c in costs:
        log(f"[衰减] δ = {c} A$/MWh …")
        b = battery.with_(degradation_cost=float(c), max_cycles_per_day=None)
        d = run_year(actual, b)
        s = summarise(d, b)
        dis_per_year = d.discharge_mwh.sum() * 365 / len(d)
        rows.append(dict(delta=c, revenue_aud_per_mw_year=s["revenue_aud_per_mw_year"],
                         cycles_per_year=s["cycles_per_year"],
                         net_of_true_degradation_per_mw_year=(s["revenue_aud_per_year"] - true_cost * dis_per_year) / b.power_mw))
    return pd.DataFrame(rows)


def duration_compare(actual: pd.DataFrame, battery: Battery, durations=(1, 2, 4), log=print) -> pd.DataFrame:
    rows = []
    for h in durations:
        log(f"[时长] {h}h …")
        b = battery.with_(energy_mwh=battery.power_mw * h)
        d = run_year(actual, b)
        s = summarise(d, b)
        rows.append(dict(duration_h=h, revenue_aud_per_mw_year=s["revenue_aud_per_mw_year"],
                         revenue_aud_per_mwh_year=s["revenue_aud_per_year"] / b.energy_mwh,
                         cycles_per_year=s["cycles_per_year"]))
    return pd.DataFrame(rows)
