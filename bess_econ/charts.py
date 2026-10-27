"""统一风格的图表。每个函数都有 lang 参数：zh（默认，中文 README 用）或 en（英文 README 用）。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd

CJK_FONT = next((_f for _f in ("Noto Sans CJK SC", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", "SimHei",
                               "WenQuanYi Zen Hei") if any(_f == f.name for f in font_manager.fontManager.ttflist)), None)
if CJK_FONT:
    plt.rcParams["font.family"] = CJK_FONT
plt.rcParams.update({"axes.unicode_minus": False, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": 0.25, "figure.dpi": 110, "savefig.dpi": 170})

BLUE, RED, GREY, GREEN, AMBER = "#2A4EC2", "#B5382A", "#8A8F98", "#1D7449", "#C08A1E"
STRAT_COLOR = {"perfect": GREY, "gbm": BLUE, "persistence": "#7C93DB", "fixed": AMBER}
STRAT_NAME = {
    "zh": {"perfect": "完美预知（上限）", "gbm": "GBM 预测 + 优化", "persistence": "昨日电价 + 优化", "fixed": "固定时段规则"},
    "en": {"perfect": "Perfect foresight\n(upper bound)", "gbm": "GBM forecast\n+ optimiser",
           "persistence": "Yesterday's prices\n+ optimiser", "fixed": "Fixed-schedule\nrule"},
}
STRAT_SHORT = {
    "zh": {"perfect": "完美预知", "gbm": "GBM 预测 + 优化", "persistence": "昨日电价 + 优化", "fixed": "固定时段"},
    "en": {"perfect": "Perfect foresight", "gbm": "GBM forecast + optimiser", "persistence": "Yesterday's prices + optimiser",
           "fixed": "Fixed schedule"},
}
MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def source_note(region: str, synthetic: bool, lang: str = "zh") -> str:
    if lang == "en":
        tag = "synthetic test data (not real prices, pipeline check only)" if synthetic else "AEMO public data"
        return f"Data: {tag}, {region}; model: bess-economics"
    tag = "合成测试数据（非真实电价，仅验证流程）" if synthetic else "AEMO 公开数据"
    return f"数据：{tag}，{region}；模型：bess-economics"


@contextmanager
def style(lang: str):
    """英文图用 DejaVu Sans（matplotlib 自带），中文图用系统里找到的中文字体。"""
    if lang == "en":
        with plt.rc_context({"font.family": "DejaVu Sans"}):
            yield
    else:
        yield


def _save(fig, path: Path, source: str):
    fig.text(0.01, 0.005, source, fontsize=7, color=GREY, ha="left", va="bottom")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def duck_curve(profile: pd.DataFrame, path: Path, region: str, source: str, lang: str = "zh"):
    en = lang == "en"
    with style(lang):
        fig, ax = plt.subplots(figsize=(8, 4.4))
        cmap = plt.get_cmap("coolwarm")
        for m in profile.columns:
            m = int(m)
            # 南半球：1 月最热（红），7 月最冷（蓝）
            ax.plot(profile.index, profile[str(m)] if str(m) in profile.columns else profile[m],
                    color=cmap(0.5 + 0.5 * np.cos(2 * np.pi * (m - 1) / 12)), lw=1.4,
                    label=MONTHS_EN[m - 1] if en else f"{m} 月")
        ax.axhline(0, color="black", lw=0.8)
        ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 3))
        if en:
            ax.set_xlabel("Time of day (AEST)"); ax.set_ylabel("Average price (A$/MWh, clipped to −500 to 1,000)")
            ax.set_title(f"{region} intraday price shape: midday solar drives prices down, the evening sets the peak",
                         fontsize=11)
        else:
            ax.set_xlabel("一天中的时刻（AEST）"); ax.set_ylabel("平均电价（A$/MWh，截断 −500~1000）")
            ax.set_title(f"{region} 日内电价曲线：午间光伏压低电价，傍晚形成高峰")
        ax.legend(ncol=6, fontsize=7, frameon=False, loc="upper left")
        _save(fig, path, source)


def spread_distribution(spreads: pd.DataFrame, path: Path, region: str, source: str, lang: str = "zh"):
    en = lang == "en"
    with style(lang):
        fig, ax = plt.subplots(figsize=(8, 4.2))
        monthly = spreads.groupby(spreads.index.month)["spread_2h"]
        data = [g.clip(upper=np.nanpercentile(spreads.spread_2h, 99)).to_numpy() for _, g in monthly]
        labels = [MONTHS_EN[m - 1] if en else f"{m}月" for m in monthly.groups.keys()]
        ax.boxplot(data, tick_labels=labels, showfliers=False,
                   medianprops=dict(color=BLUE, lw=2), boxprops=dict(color=GREY), whiskerprops=dict(color=GREY),
                   capprops=dict(color=GREY))
        med = spreads.spread_2h.median()
        if en:
            ax.set_ylabel("Daily 2-hour spread (A$/MWh)")
            ax.set_title(f"{region} daily 2-hour spread (top-2h mean − bottom-2h mean); annual median A${med:,.0f}/MWh",
                         fontsize=11)
        else:
            ax.set_ylabel("每日 2 小时价差（A$/MWh）")
            ax.set_title(f"{region} 每日 2 小时价差（最高 2h 均价 − 最低 2h 均价），全年中位数 A${med:,.0f}/MWh")
        _save(fig, path, source)


def strategy_bars(table: pd.DataFrame, path: Path, source: str, lang: str = "zh"):
    en = lang == "en"
    order = ["perfect", "gbm", "persistence", "fixed"]
    t = table.loc[order]
    with style(lang):
        fig, ax = plt.subplots(figsize=(8, 4.2))
        vals = t.revenue_aud_per_mw_year / 1e3
        bars = ax.bar([STRAT_NAME[lang][k] for k in order], vals, color=[STRAT_COLOR[k] for k in order])
        for b_, v, cr in zip(bars, vals, t.capture_rate):
            txt = f"A${v:,.0f}k\ncapture {cr*100:.0f}%" if en else f"A${v:,.0f}k\n捕获率 {cr*100:.0f}%"
            ax.text(b_.get_x() + b_.get_width() / 2, v, txt, ha="center", va="bottom", fontsize=9)
        ax.set_ylim(0, vals.max() * 1.25)
        if en:
            ax.set_ylabel("Arbitrage revenue (A$k / MW / yr)")
            ax.set_title("Same 100 MW / 200 MWh battery, same year of prices: dispatch strategy sets the revenue",
                         fontsize=11)
        else:
            ax.set_ylabel("年套利收入（A$ 千 / MW / 年）")
            ax.set_title("同一块 100 MW / 200 MWh 电池、同一年电价：调度策略决定能拿到多少钱")
        _save(fig, path, source)


def cumulative(daily: dict, path: Path, source: str, lang: str = "zh"):
    en = lang == "en"
    with style(lang):
        fig, ax = plt.subplots(figsize=(8, 4.2))
        for k in ["perfect", "gbm", "persistence", "fixed"]:
            d = daily[k]
            ax.plot(d.index, d.revenue.cumsum() / 1e6, color=STRAT_COLOR[k], lw=1.8 if k == "gbm" else 1.2,
                    label=STRAT_SHORT[lang][k])
        if en:
            ax.set_ylabel("Cumulative revenue (A$m)")
            ax.set_title("Cumulative revenue: a few extreme-price days deliver a large share of the year (the steps)",
                         fontsize=11)
        else:
            ax.set_ylabel("累计收入（A$ 百万）")
            ax.set_title("累计收入：少数极端价格日贡献了相当比例的全年收益（曲线台阶）")
        ax.legend(frameon=False)
        _save(fig, path, source)


def example_day(day, actual: pd.Series, pred: pd.Series, perfect_soc, plan_soc, path: Path, source: str,
                lang: str = "zh"):
    """perfect_soc / plan_soc：288 个区间末的储能量（MWh），也可以直接传 optimise_day 的结果（取 .soc）。"""
    en = lang == "en"
    perfect_soc = getattr(perfect_soc, "soc", perfect_soc)
    plan_soc = getattr(plan_soc, "soc", plan_soc)
    x = np.arange(len(actual)) * 5 / 60
    with style(lang):
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 5.4), sharex=True, gridspec_kw=dict(height_ratios=[1.2, 1]))
        ax1.plot(x, np.asarray(actual), color="black", lw=1.2, label="Actual price" if en else "实际电价")
        ax1.plot(x, np.asarray(pred), color=BLUE, lw=1.2, ls="--", label="GBM day-ahead forecast" if en else "GBM 次日预测")
        ax1.axhline(0, color=GREY, lw=0.6)
        ax1.set_ylabel("A$/MWh"); ax1.legend(frameon=False, fontsize=8)
        d = pd.Timestamp(day).date()
        ax1.set_title(f"Example day {d}: schedule on the forecast, settle at actual prices" if en
                      else f"{d} 调度示例：按预测做计划，按实际电价结算")
        ax2.plot(x, np.asarray(perfect_soc), color=GREY, lw=1.4, label="SOC: perfect foresight" if en else "SOC：完美预知")
        ax2.plot(x, np.asarray(plan_soc), color=BLUE, lw=1.6, label="SOC: GBM plan" if en else "SOC：GBM 计划")
        if en:
            ax2.set_ylabel("Stored energy (MWh)"); ax2.set_xlabel("Time of day (AEST)")
        else:
            ax2.set_ylabel("储能量（MWh）"); ax2.set_xlabel("一天中的时刻（AEST）")
        ax2.set_xticks(range(0, 25, 3)); ax2.legend(frameon=False, fontsize=8)
        _save(fig, path, source)


def degradation_frontier(sweep: pd.DataFrame, true_cost: float, path: Path, source: str, lang: str = "zh"):
    en = lang == "en"
    with style(lang):
        fig, ax = plt.subplots(figsize=(8, 4.2))
        ax.plot(sweep.cycles_per_year, sweep.revenue_aud_per_mw_year / 1e3, "o-", color=GREY,
                label="Gross revenue" if en else "毛收入")
        ax.plot(sweep.cycles_per_year, sweep.net_of_true_degradation_per_mw_year / 1e3, "o-", color=BLUE,
                label=f"Net of true degradation cost (A${true_cost:.0f}/MWh)" if en
                else f"扣除真实衰减成本（{true_cost:.0f} A$/MWh）后")
        for _, r in sweep.iterrows():
            ax.annotate(f"δ={r.delta:.0f}", (r.cycles_per_year, r.net_of_true_degradation_per_mw_year / 1e3),
                        textcoords="offset points", xytext=(4, -12), fontsize=8, color=BLUE)
        best = sweep.loc[sweep.net_of_true_degradation_per_mw_year.idxmax()]
        lo = min(sweep.net_of_true_degradation_per_mw_year.min(), sweep.revenue_aud_per_mw_year.min()) / 1e3
        hi = max(sweep.net_of_true_degradation_per_mw_year.max(), sweep.revenue_aud_per_mw_year.max()) / 1e3
        ax.set_ylim(lo - (hi - lo) * 0.12, hi + (hi - lo) * 0.08)
        if en:
            ax.set_xlabel("Equivalent full cycles per year"); ax.set_ylabel("A$k / MW / yr")
            ax.set_title(f"More cycling is not more profit: net revenue peaks with a wear cost of δ≈{best.delta:.0f} A$/MWh",
                         fontsize=11)
        else:
            ax.set_xlabel("年等效满循环次数"); ax.set_ylabel("A$ 千 / MW / 年")
            ax.set_title(f"多循环不等于多赚钱：调度时计入 δ≈{best.delta:.0f} A$/MWh 的衰减成本时净收益最高")
        ax.legend(frameon=False)
        _save(fig, path, source)


def duration_bars(dc: pd.DataFrame, path: Path, source: str, lang: str = "zh"):
    en = lang == "en"
    with style(lang):
        fig, ax = plt.subplots(figsize=(8, 4))
        x = np.arange(len(dc))
        ax.bar(x - 0.2, dc.revenue_aud_per_mw_year / 1e3, 0.4, color=BLUE,
               label="Per MW (A$k/yr)" if en else "每 MW（A$ 千/年）")
        ax.bar(x + 0.2, dc.revenue_aud_per_mwh_year / 1e3, 0.4, color="#9FB3E8",
               label="Per MWh (A$k/yr)" if en else "每 MWh（A$ 千/年）")
        ax.set_xticks(x); ax.set_xticklabels([f"{h:.0f}h" for h in dc.duration_h])
        ax.set_title("Longer duration earns more per MW but less per MWh (the marginal spread shrinks)" if en
                     else "时长越长每 MW 收入越高，但每 MWh 收入递减（边际价差变小）")
        ax.legend(frameon=False)
        _save(fig, path, source)


def irr_hist(sim: pd.DataFrame, summary: dict, hurdle: float, path: Path, source: str, lang: str = "zh"):
    en = lang == "en"
    with style(lang):
        fig, ax = plt.subplots(figsize=(8, 4))
        v = sim.irr.dropna() * 100
        ax.hist(v, bins=60, color="#9FB3E8", edgecolor="white")
        for q, lab in ((summary["irr_p10"], "P10"), (summary["irr_p50"], "P50"), (summary["irr_p90"], "P90")):
            ax.axvline(q * 100, color=BLUE, lw=1.2)
            ax.text(q * 100, ax.get_ylim()[1] * 0.92, f"{lab} {q*100:.1f}%", color=BLUE, ha="center", fontsize=8,
                    bbox=dict(fc="white", ec="none", pad=1))
        ax.axvline(hurdle * 100, color=RED, ls="--", lw=1)
        p = summary["prob_irr_above_hurdle"] * 100
        if en:
            ax.set_xlabel("Project IRR (%, pre-tax real)"); ax.set_ylabel("Simulations")
            ax.set_title(f"IRR distribution from energy arbitrage alone: {p:.0f}% chance of clearing the {hurdle*100:.0f}% hurdle",
                         fontsize=11)
        else:
            ax.set_xlabel("项目 IRR（%，税前实际）"); ax.set_ylabel("模拟次数")
            ax.set_title(f"只靠能量套利的 IRR 分布：超过 {hurdle*100:.0f}% 门槛的概率 {p:.0f}%")
        _save(fig, path, source)
