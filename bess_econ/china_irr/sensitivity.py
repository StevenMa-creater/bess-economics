"""敏感性分析与图表。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd

from .model import Assumptions, evaluate, lifetime_revenue_delta, WAN

# 中文字体：优先 Noto Sans CJK，缺失时退回默认（图中中文会变方块，但不报错）
for _f in ("Noto Sans CJK SC", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", "WenQuanYi Zen Hei"):
    if any(_f == f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False

# 敏感性因子：(显示名, 参数名, 低值, 高值, 是否为相对变化)
FACTORS = [
    ("系统效率 RTE ±2pp", "rte", -0.02, +0.02, "abs"),
    ("放电电价 ±0.10 元/kWh", "discharge_price_yuan_per_kwh", -0.10, +0.10, "abs"),
    ("充电电价 ±0.05 元/kWh", "charge_price_yuan_per_kwh", +0.05, -0.05, "abs"),  # 充电贵 = 坏
    ("日循环次数 ±0.3", "cycles_per_day", -0.3, +0.3, "abs"),
    ("初始投资 ±10%", "capex_system_yuan_per_wh", +0.10, -0.10, "rel"),       # 投资高 = 坏
    ("容量电价考核 70% / 100%", "capacity_price_compliance", 0.70, 1.00, "set"),
    ("年衰减 3.5% / 1.5%", "degradation_per_year", 0.035, 0.015, "set"),
    ("贷款利率 4.5% / 2.5%", "loan_rate", 0.045, 0.025, "set"),
]


FACTOR_EN = {
    "系统效率 RTE ±2pp": "Round-trip efficiency ±2 pp",
    "放电电价 ±0.10 元/kWh": "Discharge price ±0.10 CNY/kWh",
    "充电电价 ±0.05 元/kWh": "Charging price ±0.05 CNY/kWh",
    "日循环次数 ±0.3": "Cycles per day ±0.3",
    "初始投资 ±10%": "Capex ±10%",
    "容量电价考核 70% / 100%": "Capacity-price compliance 70% / 100%",
    "年衰减 3.5% / 1.5%": "Annual degradation 3.5% / 1.5%",
    "贷款利率 4.5% / 2.5%": "Loan rate 4.5% / 2.5%",
}
PRESET_EN = {"shandong": "Shandong", "gansu": "Gansu", "guangdong": "Guangdong", "no_capacity_price": "No capacity price"}


@contextmanager
def _style(lang: str):
    if lang == "en":
        with plt.rc_context({"font.family": "DejaVu Sans"}):
            yield
    else:
        yield


def _apply(a: Assumptions, param: str, val: float, mode: str) -> Assumptions:
    cur = getattr(a, param)
    if mode == "abs":
        return a.with_(**{param: cur + val})
    if mode == "rel":
        return a.with_(**{param: cur * (1 + val)})
    return a.with_(**{param: val})


def tornado(a: Assumptions, metric: str = "irr_project_posttax") -> pd.DataFrame:
    base = getattr(evaluate(a), metric)
    rows = []
    for label, param, lo, hi, mode in FACTORS:
        v_lo = getattr(evaluate(_apply(a, param, lo, mode)), metric)
        v_hi = getattr(evaluate(_apply(a, param, hi, mode)), metric)
        rows.append(dict(factor=label, low=v_lo, high=v_hi, base=base, swing=abs(v_hi - v_lo)))
    return pd.DataFrame(rows).sort_values("swing", ascending=True)


def plot_tornado(df: pd.DataFrame, title: str, path: Path, lang: str = "zh") -> None:
    with _style(lang):
        _plot_tornado(df, title, path, lang)


def _plot_tornado(df: pd.DataFrame, title: str, path: Path, lang: str) -> None:
    en = lang == "en"
    fig, ax = plt.subplots(figsize=(8, 4.8))
    base = df.base.iloc[0] * 100
    y = np.arange(len(df))
    for i, r in enumerate(df.itertuples()):
        lo, hi = r.low * 100, r.high * 100
        ax.barh(i, hi - base, left=base, color="#2A4EC2", alpha=0.85)
        ax.barh(i, lo - base, left=base, color="#B5382A", alpha=0.85)
        ax.text(min(lo, hi) - 0.15, i, f"{min(lo, hi):.1f}%", va="center", ha="right", fontsize=8)
        ax.text(max(lo, hi) + 0.15, i, f"{max(lo, hi):.1f}%", va="center", ha="left", fontsize=8)
    ax.axvline(base, color="black", lw=1)
    lo_all, hi_all = min(df.low.min(), df.high.min()) * 100, max(df.low.max(), df.high.max()) * 100
    ax.set_xlim(lo_all - 1.5, hi_all + 1.5)
    ax.set_yticks(y)
    ax.set_yticklabels([FACTOR_EN.get(f, f) for f in df.factor] if en else df.factor)
    if en:
        ax.set_xlabel("Project IRR, post-tax (%)")
        ax.set_title(f"{title}\nBase IRR {base:.1f}%; red = downside, blue = upside")
    else:
        ax.set_xlabel("全投资税后 IRR（%）")
        ax.set_title(f"{title}\n基准 IRR {base:.1f}%，红=不利，蓝=有利")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def province_compare(presets: dict[str, Assumptions], durations=(2.0, 4.0), power_mw=100.0) -> pd.DataFrame:
    rows = []
    for key, a in presets.items():
        for d in durations:
            b = a.with_(duration_h=d, power_mw=power_mw)
            r = evaluate(b, hurdle=0.06)
            rows.append(dict(preset=key, name=a.name, duration_h=d, capex_wan=r.capex_wan,
                             capacity_price=r.capacity_price, revenue_y1_wan=r.revenue_year1_wan,
                             breakeven_revenue_wan=r.breakeven_revenue_wan,
                             irr_pretax=r.irr_project_pretax, irr_posttax=r.irr_project_posttax,
                             irr_equity=r.irr_equity, payback=r.payback_project_years,
                             lcos=r.lcos_yuan_per_kwh,
                             mix_energy=r.revenue_mix_year1["rev_energy"],
                             mix_capacity=r.revenue_mix_year1["rev_capacity"]))
    return pd.DataFrame(rows)


def plot_compare(df: pd.DataFrame, path: Path, hurdle: float = 0.06, lang: str = "zh") -> None:
    with _style(lang):
        _plot_compare(df, path, hurdle, lang)


def _plot_compare(df: pd.DataFrame, path: Path, hurdle: float, lang: str) -> None:
    en = lang == "en"
    fig, ax = plt.subplots(figsize=(8, 4.5))
    names = df.name.unique()
    x = np.arange(len(names))
    w = 0.38
    for j, d in enumerate(sorted(df.duration_h.unique())):
        sub = df[df.duration_h == d].set_index("name").loc[names]
        vals = sub.irr_posttax * 100
        bars = ax.bar(x + (j - 0.5) * w, vals, w, label=f"{d:.0f}h system" if en else f"{d:.0f}h 系统",
                      color="#2A4EC2" if j else "#9FB3E8")
        for b_, v in zip(bars, vals):
            ax.text(b_.get_x() + b_.get_width() / 2, v + 0.15, f"{v:.1f}", ha="center", fontsize=8)
    ax.axhline(hurdle * 100, color="#B5382A", ls="--", lw=1, label=f"Hurdle {hurdle*100:.0f}%" if en else f"门槛 {hurdle*100:.0f}%")
    ax.set_xticks(x)
    if en:
        key = df.drop_duplicates("name").set_index("name").preset
        ax.set_xticklabels([PRESET_EN.get(key[n], n) for n in names])
        ax.set_ylabel("Project IRR, post-tax (%)")
        ax.set_title("100 MW independent storage: IRR by province and duration (2026 inputs)")
    else:
        ax.set_xticklabels(names)
        ax.set_ylabel("全投资税后 IRR（%）")
        ax.set_title("100 MW 独立储能：各省 / 时长 IRR 对比（2026 年参数）")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def efficiency_value(a: Assumptions, dpp: float = 0.01) -> dict:
    """效率每提高 dpp，全生命周期名义收入增加多少（万元）及 IRR 变化。"""
    delta_rev = lifetime_revenue_delta(a, rte=a.rte + dpp)
    irr0 = evaluate(a).irr_project_posttax
    irr1 = evaluate(a.with_(rte=a.rte + dpp)).irr_project_posttax
    return dict(delta_lifetime_revenue_wan=round(delta_rev, 0), irr_base=irr0, irr_new=irr1)
