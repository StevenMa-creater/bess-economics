"""情景实验：只改电池的一两个参数，和基准情景比较收益、循环、净收益和 IRR。

基准：100 MW / 200 MWh（2 小时）、RTE 86%、每天最多 1.5 次循环，即 `python run.py nem` 的结果。

    python run.py scenario --hours 4                         # 换成 4 小时电池
    python run.py scenario --max-cycles 1.0                  # 循环上限从 1.5 降到 1.0
    python run.py scenario --hours 4 --max-cycles 1.0 --note "我发现……"

第一次运行会训练 GBM 预测并缓存（几分钟），之后每个情景只重跑两次全年 MILP（完美预知 + GBM）。
结果写入 outputs/scenarios/<名称>.json，再运行 `python scripts/update_readme.py` 就会出现在 README 里。
同一个名称、同样参数再跑一次（比如只改 --note）会直接读已有的逐日结果，几秒钟完成。

两种 IRR 口径：
- 毛收入口径：和 `run.py nem` 的蒙特卡洛完全一致。容量每年固定衰减 2%，不管循环多少次，
  所以"多循环"在这个口径下没有成本。
- 全寿命口径：每放 1 MWh 计提 30 A$ 用于补充电芯（augmentation），容量因此保持不衰减。
  循环越多、计提越多，比较循环上限时看这一列才公平。
"""
from __future__ import annotations

import json
from dataclasses import replace
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from . import forecast as F
from . import montecarlo as MC
from .dispatch import Battery, run_year
from .nem import data as D

TRUE_DEGRADATION_COST = 30.0   # A$/MWh 放电量，与 README 衰减权衡一致

# 两段式投资成本：功率部分（变流器、升压站、并网）按 kW，电池部分按 kWh。
# 校准：2h 众数 520 A$/kWh（基准），4h 众数 380 A$/kWh（GenCost 2024-25 草案 4h 约 423，2025-26 再降 11–16%）。
# 由 P + 2E = 1040、P + 4E = 1520 解得 P = 560 A$/kW，E = 240 A$/kWh。
POWER_COST_PER_KW = 560.0
ENERGY_COST_PER_KWH = 240.0
BASE_CAPEX = (420.0, 520.0, 650.0)


def capex_for_duration(hours: float) -> tuple:
    """返回 (低, 众数, 高) A$/kWh。2 小时时恰好等于基准 (420, 520, 650)。"""
    f = (POWER_COST_PER_KW / hours + ENERGY_COST_PER_KWH) / (POWER_COST_PER_KW / 2 + ENERGY_COST_PER_KWH)
    return tuple(round(x * f, 1) for x in BASE_CAPEX)


def _cache(path: Path, build):
    if path.exists():
        try:
            return pd.read_pickle(path)
        except Exception:          # pandas 版本不同读不了缓存时，重新计算
            pass
    obj = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.to_pickle(obj, path)
    return obj


def load_inputs(root: Path, region: str = "QLD1", test_start: str | None = None, log=print):
    """价格矩阵、负荷矩阵、GBM 次日预测（都缓存，第二次起秒读）。"""
    def build_mats():
        log("[数据] 第一次运行：读取 data/raw 并清洗 …")
        df = D.load(root / "data" / "raw", region)
        return D.to_matrix(df, "rrp"), D.to_matrix(df, "demand")

    P, Dm = _cache(root / "data" / "cache" / f"{region}_matrices.pkl", build_mats)
    ts = pd.Timestamp(test_start) if test_start else pd.Timestamp(f"{P.index.max().year}-01-01")

    def build_pred():
        log(f"[预测] 第一次运行：按月滚动训练 GBM（测试期从 {ts.date()} 开始），完成后会缓存 …")
        return F.walk_forward_gbm(P, Dm, ts)

    pred = _cache(root / "data" / "cache" / f"{region}_gbm_{ts.date()}.pkl", build_pred)
    return P, pred


def _run(args):
    actual, b, plan = args
    return run_year(actual, b, price_plan=plan)


def dispatch_both(actual: pd.DataFrame, pred: pd.DataFrame, b: Battery) -> dict:
    """完美预知和 GBM 两次全年调度，两个进程并行。"""
    with ProcessPoolExecutor(max_workers=2) as ex:
        perfect, gbm = ex.map(_run, [(actual, b, None), (actual, b, pred)])
    return {"perfect": perfect, "gbm": gbm}


def metrics(daily: dict, b: Battery, au: MC.AUAssumptions, mc_runs: int = 5000) -> dict:
    scale = 365 / len(daily["gbm"])
    perfect_rev = daily["perfect"].revenue.sum()
    out = {}
    for k, d in daily.items():
        rev = d.revenue.sum() * scale
        dis = d.discharge_mwh.sum() * scale
        out[k] = dict(revenue_k_per_mw=rev / b.power_mw / 1e3,
                      net_k_per_mw=(rev - TRUE_DEGRADATION_COST * dis) / b.power_mw / 1e3,
                      cycles_per_year=d.cycles.sum() * scale,
                      capture_rate=d.revenue.sum() / perfect_rev)
    g = daily["gbm"]
    gross = MC.summarise(MC.simulate(g.revenue, au, n=mc_runs), au)
    au_aug = replace(au, capacity_fade_per_year=0.0)   # 计提补电芯费用后容量保持
    net = MC.summarise(MC.simulate(g.revenue - TRUE_DEGRADATION_COST * g.discharge_mwh, au_aug, n=mc_runs), au_aug)
    out["irr"] = dict(gross_p10=gross["irr_p10"], gross_p50=gross["irr_p50"], gross_p90=gross["irr_p90"],
                      gross_prob_above_hurdle=gross["prob_irr_above_hurdle"],
                      net_p50=net["irr_p50"], net_prob_above_hurdle=net["prob_irr_above_hurdle"])
    out["capex_mid_aud_m"] = au.capex_aud_per_kwh[1] * au.energy_mwh / 1e3
    out["breakeven_k_per_mw"] = MC.breakeven_revenue_per_mw(au) / 1e3
    return out


def _baseline_daily(root: Path, actual: pd.DataFrame, pred: pd.DataFrame, base: Battery, log) -> dict:
    """优先读 run.py nem 已经算好的逐日结果，没有再重算。"""
    nem = root / "outputs" / "nem"
    files = {k: nem / f"daily_{k}.csv" for k in ("perfect", "gbm")}
    if all(f.exists() for f in files.values()):
        daily = {k: pd.read_csv(f, index_col=0, parse_dates=True) for k, f in files.items()}
        if len(daily["gbm"]) == len(actual):
            return daily
    log("[基准] 没找到 outputs/nem 的逐日结果，重新计算基准 …")
    return dispatch_both(actual, pred, base)


def _load_scenario_daily(out: Path, name: str, params: dict, n_days: int):
    j = out / f"{name}.json"
    files = {k: out / "daily" / f"{name}_{k}.csv" for k in ("perfect", "gbm")}
    if not (j.exists() and all(f.exists() for f in files.values())):
        return None
    old = json.loads(j.read_text(encoding="utf-8"))["params"]
    if any(old.get(k) != v for k, v in params.items()):
        return None
    daily = {k: pd.read_csv(f, index_col=0, parse_dates=True) for k, f in files.items()}
    return daily if len(daily["gbm"]) == n_days else None


def run(root: Path, hours: float = 2.0, max_cycles: float | None = 1.5, rte: float = 0.86,
        capex: tuple | None = None, name: str | None = None, note: str = "", region: str = "QLD1",
        test_start: str | None = None, mc_runs: int = 5000, log=print) -> dict:
    P, pred = load_inputs(root, region, test_start, log)
    actual = P.loc[pred.index]

    base_b = Battery()
    base_au = MC.AUAssumptions()
    b = base_b.with_(energy_mwh=base_b.power_mw * hours, max_cycles_per_day=max_cycles, rte=rte)
    au = MC.AUAssumptions(duration_h=hours, capex_aud_per_kwh=tuple(capex) if capex else capex_for_duration(hours))

    cyc = "不限" if max_cycles is None else f"{max_cycles:g}"
    label = f"{hours:g}h / 循环上限 {cyc} / RTE {rte:.0%}"
    name = name or f"{hours:g}h_cycles{cyc if max_cycles is not None else 'none'}_rte{rte*100:.0f}"
    log(f"[情景] {label}；投资成本 {au.capex_aud_per_kwh} A$/kWh")

    log("[基准] 2h / 1.5 循环 …")
    base = metrics(_baseline_daily(root, actual, pred, base_b, log), base_b, base_au, mc_runs)
    out = root / "outputs" / "scenarios"
    out.mkdir(parents=True, exist_ok=True)
    params = dict(hours=hours, max_cycles_per_day=max_cycles, rte=rte)
    daily = _load_scenario_daily(out, name, params, len(actual))
    if daily is None:
        log(f"[调度] 情景的完美预知 + GBM，各 {len(actual)} 天（约几分钟）…")
        daily = dispatch_both(actual, pred, b)
        (out / "daily").mkdir(parents=True, exist_ok=True)
        for k, d in daily.items():
            d.to_csv(out / "daily" / f"{name}_{k}.csv")
    else:
        log("[调度] 已有同参数的逐日结果，直接读取")
    scen = metrics(daily, b, au, mc_runs)

    result = dict(name=name, label=label, note=note, region=region, test_start=str(pred.index.min().date()),
                  test_days=int(len(actual)),
                  params={**params, "capex_aud_per_kwh": list(au.capex_aud_per_kwh)},
                  baseline=base, scenario=scen)
    (out / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print_table(result, log)
    log(f"[完成] 已保存 outputs/scenarios/{name}.json；运行 python scripts/update_readme.py 写进 README")
    return result


ROWS = [  # (显示名, 取值函数, 格式)
    ("完美预知收入（千 A$/MW/年）", lambda m: m["perfect"]["revenue_k_per_mw"], "{:.0f}"),
    ("GBM 策略收入（千 A$/MW/年）", lambda m: m["gbm"]["revenue_k_per_mw"], "{:.0f}"),
    ("GBM 捕获率", lambda m: m["gbm"]["capture_rate"] * 100, "{:.0f}%"),
    ("GBM 年循环次数", lambda m: m["gbm"]["cycles_per_year"], "{:.0f}"),
    ("GBM 扣衰减净收入（千 A$/MW/年）", lambda m: m["gbm"]["net_k_per_mw"], "{:.0f}"),
    ("投资（百万 A$，众数）", lambda m: m["capex_mid_aud_m"], "{:.0f}"),
    ("IRR P50 · 毛收入口径", lambda m: m["irr"]["gross_p50"] * 100, "{:.1f}%"),
    ("IRR P10 / P90 · 毛收入口径", lambda m: (m["irr"]["gross_p10"] * 100, m["irr"]["gross_p90"] * 100), "{:.1f}% / {:.1f}%"),
    ("IRR > 8% 的概率 · 毛收入口径", lambda m: m["irr"]["gross_prob_above_hurdle"] * 100, "{:.0f}%"),
    ("IRR P50 · 全寿命口径（计提补电芯）", lambda m: m["irr"]["net_p50"] * 100, "{:.1f}%"),
    ("7% IRR 所需第 1 年收入（千 A$/MW）", lambda m: m["breakeven_k_per_mw"], "{:.0f}"),
]


def fmt(f, v):
    return f.format(*v) if isinstance(v, tuple) else f.format(v)


def rows(result: dict):
    for title, get, f in ROWS:
        yield title, fmt(f, get(result["baseline"])), fmt(f, get(result["scenario"]))


def _w(s: str) -> int:  # 显示宽度：中文占 2 格
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int, right: bool = False) -> str:
    gap = " " * max(0, n - _w(s))
    return gap + s if right else s + gap


def print_table(result: dict, log=print):
    log("\n" + _pad("指标", 36) + _pad("基准 2h/1.5", 16, True) + _pad(result["label"], 30, True))
    for t, a, b in rows(result):
        log(_pad(t, 36) + _pad(a, 16, True) + _pad(b, 30, True))
