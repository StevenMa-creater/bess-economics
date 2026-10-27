"""统一风格的图表。"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd

for _f in ("Noto Sans CJK SC", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", "SimHei", "WenQuanYi Zen Hei"):
    if any(_f == f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = _f
        break
plt.rcParams.update({"axes.unicode_minus": False, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": 0.25, "figure.dpi": 110, "savefig.dpi": 170})

BLUE, RED, GREY, GREEN, AMBER = "#2A4EC2", "#B5382A", "#8A8F98", "#1D7449", "#C08A1E"
STRAT_COLOR = {"perfect": GREY, "gbm": BLUE, "persistence": "#7C93DB", "fixed": AMBER}


def _save(fig, path: Path, source: str):
    fig.text(0.01, 0.005, source, fontsize=7, color=GREY, ha="left", va="bottom")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path)
    plt.close(fig)


def duck_curve(profile: pd.DataFrame, path: Path, region: str, source: str):
    fig, ax = plt.subplots(figsize=(8, 4.4))
    cmap = plt.get_cmap("coolwarm")
    for m in profile.columns:
        # 南半球：1 月最热（红），7 月最冷（蓝）
        ax.plot(profile.index, profile[m], color=cmap(0.5 + 0.5 * np.cos(2 * np.pi * (m - 1) / 12)), lw=1.4,
                label=f"{m} 月")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 3))
    ax.set_xlabel("一天中的时刻（AEST）"); ax.set_ylabel("平均电价（A$/MWh，截断 −500~1000）")
    ax.set_title(f"{region} 日内电价曲线：午间光伏压低电价，傍晚形成高峰")
    ax.legend(ncol=6, fontsize=7, frameon=False, loc="upper left")
    _save(fig, path, source)


def spread_distribution(spreads: pd.DataFrame, path: Path, region: str, source: str):
    fig, ax = plt.subplots(figsize=(8, 4.2))
    monthly = spreads.groupby(spreads.index.month)["spread_2h"]
    data = [g.clip(upper=np.nanpercentile(spreads.spread_2h, 99)).to_numpy() for _, g in monthly]
    ax.boxplot(data, tick_labels=[f"{m}月" for m in monthly.groups.keys()], showfliers=False,
               medianprops=dict(color=BLUE, lw=2), boxprops=dict(color=GREY), whiskerprops=dict(color=GREY),
               capprops=dict(color=GREY))
    ax.set_ylabel("每日 2 小时价差（A$/MWh）")
    med = spreads.spread_2h.median()
    ax.set_title(f"{region} 每日 2 小时价差（最高 2h 均价 − 最低 2h 均价），全年中位数 A${med:,.0f}/MWh")
    _save(fig, path, source)


def strategy_bars(table: pd.DataFrame, path: Path, source: str):
    order = ["perfect", "gbm", "persistence", "fixed"]
    t = table.loc[order]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    vals = t.revenue_aud_per_mw_year / 1e3
    bars = ax.bar(t.name, vals, color=[STRAT_COLOR[k] for k in order])
    for b_, v, cr in zip(bars, vals, t.capture_rate):
        ax.text(b_.get_x() + b_.get_width() / 2, v, f"A${v:,.0f}k\n捕获率 {cr*100:.0f}%", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("年套利收入（A$ 千 / MW / 年）")
    ax.set_ylim(0, vals.max() * 1.25)
    ax.set_title("同一块 100 MW / 200 MWh 电池、同一年电价：调度策略决定能拿到多少钱")
    _save(fig, path, source)


def cumulative(daily: dict, path: Path, source: str):
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for k in ["perfect", "gbm", "persistence", "fixed"]:
        d = daily[k]
        ax.plot(d.index, d.revenue.cumsum() / 1e6, color=STRAT_COLOR[k], lw=1.8 if k == "gbm" else 1.2,
                label={"perfect": "完美预知", "gbm": "GBM 预测 + 优化", "persistence": "昨日电价 + 优化", "fixed": "固定时段"}[k])
    ax.set_ylabel("累计收入（A$ 百万）")
    ax.set_title("累计收入：少数极端价格日贡献了相当比例的全年收益（曲线台阶）")
    ax.legend(frameon=False)
    _save(fig, path, source)


def example_day(day, actual: pd.Series, pred: pd.Series, perfect, plan, path: Path, source: str):
    x = np.arange(len(actual)) * 5 / 60
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 5.4), sharex=True, gridspec_kw=dict(height_ratios=[1.2, 1]))
    ax1.plot(x, actual.to_numpy(), color="black", lw=1.2, label="实际电价")
    ax1.plot(x, pred.to_numpy(), color=BLUE, lw=1.2, ls="--", label="GBM 次日预测")
    ax1.axhline(0, color=GREY, lw=0.6)
    ax1.set_ylabel("A$/MWh"); ax1.legend(frameon=False, fontsize=8)
    ax1.set_title(f"{pd.Timestamp(day).date()} 调度示例：按预测做计划，按实际电价结算")
    ax2.plot(x, perfect.soc, color=GREY, lw=1.4, label="SOC：完美预知")
    ax2.plot(x, plan.soc, color=BLUE, lw=1.6, label="SOC：GBM 计划")
    ax2.set_ylabel("储能量（MWh）"); ax2.set_xlabel("一天中的时刻（AEST）")
    ax2.set_xticks(range(0, 25, 3)); ax2.legend(frameon=False, fontsize=8)
    _save(fig, path, source)


def degradation_frontier(sweep: pd.DataFrame, true_cost: float, path: Path, source: str):
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.plot(sweep.cycles_per_year, sweep.revenue_aud_per_mw_year / 1e3, "o-", color=GREY, label="毛收入")
    ax.plot(sweep.cycles_per_year, sweep.net_of_true_degradation_per_mw_year / 1e3, "o-", color=BLUE,
            label=f"扣除真实衰减成本（{true_cost:.0f} A$/MWh）后")
    for _, r in sweep.iterrows():
        ax.annotate(f"δ={r.delta:.0f}", (r.cycles_per_year, r.net_of_true_degradation_per_mw_year / 1e3),
                    textcoords="offset points", xytext=(4, -12), fontsize=8, color=BLUE)
    best = sweep.loc[sweep.net_of_true_degradation_per_mw_year.idxmax()]
    lo = min(sweep.net_of_true_degradation_per_mw_year.min(), sweep.revenue_aud_per_mw_year.min()) / 1e3
    hi = max(sweep.net_of_true_degradation_per_mw_year.max(), sweep.revenue_aud_per_mw_year.max()) / 1e3
    ax.set_ylim(lo - (hi - lo) * 0.12, hi + (hi - lo) * 0.08)
    ax.set_xlabel("年等效满循环次数"); ax.set_ylabel("A$ 千 / MW / 年")
    ax.set_title(f"多循环不等于多赚钱：调度时计入 δ≈{best.delta:.0f} A$/MWh 的衰减成本时净收益最高")
    ax.legend(frameon=False)
    _save(fig, path, source)


def duration_bars(dc: pd.DataFrame, path: Path, source: str):
    fig, ax = plt.subplots(figsize=(8, 4))
    x = np.arange(len(dc))
    ax.bar(x - 0.2, dc.revenue_aud_per_mw_year / 1e3, 0.4, color=BLUE, label="每 MW（A$ 千/年）")
    ax.bar(x + 0.2, dc.revenue_aud_per_mwh_year / 1e3, 0.4, color="#9FB3E8", label="每 MWh（A$ 千/年）")
    ax.set_xticks(x); ax.set_xticklabels([f"{h:.0f}h" for h in dc.duration_h])
    ax.set_title("时长越长每 MW 收入越高，但每 MWh 收入递减（边际价差变小）")
    ax.legend(frameon=False)
    _save(fig, path, source)


def irr_hist(sim: pd.DataFrame, summary: dict, hurdle: float, path: Path, source: str):
    fig, ax = plt.subplots(figsize=(8, 4))
    v = sim.irr.dropna() * 100
    ax.hist(v, bins=60, color="#9FB3E8", edgecolor="white")
    for q, lab in ((summary["irr_p10"], "P10"), (summary["irr_p50"], "P50"), (summary["irr_p90"], "P90")):
        ax.axvline(q * 100, color=BLUE, lw=1.2)
        ax.text(q * 100, ax.get_ylim()[1] * 0.92, f"{lab} {q*100:.1f}%", color=BLUE, ha="center", fontsize=8,
                bbox=dict(fc="white", ec="none", pad=1))
    ax.axvline(hurdle * 100, color=RED, ls="--", lw=1)
    ax.set_xlabel("项目 IRR（%，税前实际）"); ax.set_ylabel("模拟次数")
    ax.set_title(f"只靠能量套利的 IRR 分布：超过 {hurdle*100:.0f}% 门槛的概率 {summary['prob_irr_above_hurdle']*100:.0f}%")
    _save(fig, path, source)
