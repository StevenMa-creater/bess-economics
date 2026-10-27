"""一键跑完 NEM 全流程：数据 → 市场画像 → 预测 → 调度回测 → 衰减权衡 → 时长对比 → 蒙特卡洛 IRR。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as BT
from . import montecarlo as MC
from . import redraw
from .dispatch import Battery, optimise_day
from .nem import data as D
from .nem import explore as X


def run(raw_dir: Path, out: Path, region: str = "QLD1", test_start: str | None = None,
        synthetic: bool = False, mc_runs: int = 5000, log=print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tag = "合成测试数据（非真实电价，仅验证流程）" if synthetic else "AEMO 公开数据"

    notes: list[str] = []
    df = D.load(raw_dir, region, log=notes)
    for n in notes:
        log(n)
    v = D.validate(df)
    log(f"[数据] {v['first']} → {v['last']}，{v['days']} 天，{v['rows']:,} 个 5 分钟区间")

    P = D.to_matrix(df, "rrp")
    Dm = D.to_matrix(df, "demand")

    # ---- 测试期：默认取最后一个完整自然年，或数据开始 120 天之后 ----
    if test_start is None:
        last_year = P.index.max().year
        ts = pd.Timestamp(f"{last_year}-01-01")
        if (ts - P.index.min()).days < 90:
            ts = P.index.min() + pd.Timedelta(days=120)
            ts = pd.Timestamp(ts.year, ts.month, 1) + pd.offsets.MonthBegin(1)
    else:
        ts = pd.Timestamp(test_start)
    test_df = df[df.trade_date >= ts]

    # ---- 市场画像（测试期）----
    profile = X.market_profile(test_df)
    spreads = X.daily_spreads(P.loc[P.index >= ts])
    X.monthly_profile(test_df).to_csv(out / "monthly_profile.csv")
    spreads.to_csv(out / "daily_spreads.csv")

    # ---- 回测 ----
    battery = Battery()
    bt = BT.run_backtest(P, Dm, battery, ts, log=log)
    table = bt["table"]
    table.to_csv(out / "strategy_table.csv")
    for k, d in bt["daily"].items():
        d.to_csv(out / f"daily_{k}.csv")

    # 示例日：GBM 捕获率在中位数附近、且价差不小的一天
    g, p = bt["daily"]["gbm"].revenue, bt["daily"]["perfect"].revenue
    ratio = (g / p.replace(0, np.nan)).dropna()
    cand = ratio[(p.loc[ratio.index] > p.median())]
    day = (cand - cand.median()).abs().idxmin() if len(cand) else p.idxmax()
    perf = optimise_day(bt["actual"].loc[day].to_numpy(), battery)
    plan = optimise_day(bt["pred"].loc[day].to_numpy(), battery, bt["actual"].loc[day].to_numpy())
    pd.DataFrame(dict(actual=bt["actual"].loc[day].to_numpy(), pred=bt["pred"].loc[day].to_numpy(),
                      soc_perfect=perf.soc, soc_plan=plan.soc)).to_csv(out / "example_day.csv", index=False)

    # ---- 衰减权衡、时长对比（完美预知口径）----
    true_cost = 30.0
    sweep = BT.degradation_sweep(bt["actual"], battery, true_cost=true_cost, log=log)
    sweep.to_csv(out / "degradation_sweep.csv", index=False)
    dur = BT.duration_compare(bt["actual"], battery, log=log)
    dur.to_csv(out / "duration_compare.csv", index=False)

    # ---- 蒙特卡洛 IRR（GBM 策略的真实可实现收益）----
    au = MC.AUAssumptions()
    log(f"[蒙特卡洛] {mc_runs} 次 …")
    sim = MC.simulate(bt["daily"]["gbm"].revenue, au, n=mc_runs)
    mc = MC.summarise(sim, au)
    mc["breakeven_revenue_aud_per_mw_year"] = MC.breakeven_revenue_per_mw(au)
    sim[["irr"]].to_csv(out / "mc_irr.csv", index=False)

    summary = dict(
        data_source=tag, region=region, data=v, test_start=str(ts.date()), test_days=int(len(bt["actual"])),
        battery=dict(power_mw=battery.power_mw, energy_mwh=battery.energy_mwh, rte=battery.rte,
                     max_cycles_per_day=battery.max_cycles_per_day),
        market=profile, median_daily_spread_2h=float(spreads.spread_2h.median()),
        naive_estimate=X.naive_arbitrage_estimate(spreads, battery.power_mw, 2, battery.rte),
        forecast_metrics=bt["forecast_metrics"],
        strategies=table[["name", "revenue_aud_per_mw_year", "capture_rate", "cycles_per_year",
                          "avg_spread_captured"]].to_dict(orient="index"),
        degradation=sweep.to_dict(orient="records"), durations=dur.to_dict(orient="records"),
        au_assumptions={k: v for k, v in au.__dict__.items()}, montecarlo=mc, example_day=str(pd.Timestamp(day).date()),
    )
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    for lang in ("zh", "en"):
        redraw.nem_charts(out, lang, log=log)
    log(f"[完成] 结果在 {out}")
    return summary
