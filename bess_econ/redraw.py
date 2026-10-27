"""只用 outputs/nem/ 里保存的结果重画 NEM 图表，不需要原始电价、不重跑优化。

pipeline 跑完会自动调用；改了图表样式或要补某种语言时，也可以单独运行：
    python run.py charts            # 中文 → outputs/nem/，英文 → outputs/nem/en/
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import charts as C
from . import montecarlo as MC

STRATS = ("perfect", "gbm", "persistence", "fixed")


def _csv(path: Path, **kw) -> pd.DataFrame:
    return pd.read_csv(path, **kw)


def nem_charts(out: Path, lang: str = "zh", log=print) -> list[str]:
    """读取 out/ 下的 csv 和 summary.json，把图画到 out/（zh）或 out/en/（en）。返回没画成的图（缺中间数据时）。"""
    out = Path(out)
    s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    region, synthetic = s["region"], s["data_source"] != "AEMO 公开数据"
    src = C.source_note(region, synthetic, lang)
    dest = out if lang == "zh" else out / lang
    dest.mkdir(parents=True, exist_ok=True)
    skipped = []

    prof = out / "monthly_profile.csv"
    if prof.exists():
        C.duck_curve(_csv(prof, index_col=0), dest / "01_duck_curve.png", region, src, lang)
    else:
        skipped.append("01_duck_curve.png（缺 monthly_profile.csv，需原始电价重跑 python run.py nem）")

    spreads = _csv(out / "daily_spreads.csv", index_col=0, parse_dates=True)
    C.spread_distribution(spreads, dest / "02_daily_spread.png", region, src, lang)

    table = _csv(out / "strategy_table.csv", index_col=0)
    C.strategy_bars(table, dest / "03_strategy_capture.png", src, lang)

    daily = {k: _csv(out / f"daily_{k}.csv", index_col=0, parse_dates=True) for k in STRATS}
    C.cumulative(daily, dest / "04_cumulative_revenue.png", src, lang)

    ex = out / "example_day.csv"
    if ex.exists():
        e = _csv(ex)
        C.example_day(s["example_day"], e.actual, e.pred, e.soc_perfect, e.soc_plan, dest / "05_example_day.png", src, lang)
    else:
        skipped.append("05_example_day.png（缺 example_day.csv，需原始电价重跑 python run.py nem）")

    C.degradation_frontier(_csv(out / "degradation_sweep.csv"), 30.0, dest / "06_degradation_tradeoff.png", src, lang)
    C.duration_bars(_csv(out / "duration_compare.csv"), dest / "07_duration.png", src, lang)

    mc_file = out / "mc_irr.csv"
    if mc_file.exists():
        sim = _csv(mc_file)
    else:  # 旧版输出没有保存模拟结果：用同一随机种子从逐日收益重新模拟（结果与 summary 一致）
        au = MC.AUAssumptions(**{k: tuple(v) if isinstance(v, list) else v for k, v in s["au_assumptions"].items()})
        sim = MC.simulate(daily["gbm"].revenue, au, n=s["montecarlo"]["n"])
        sim[["irr"]].to_csv(mc_file, index=False)
    C.irr_hist(sim, s["montecarlo"], s["au_assumptions"]["hurdle"], dest / "08_irr_distribution.png", src, lang)

    for msg in skipped:
        log(f"[图表] 跳过 {msg}")
    log(f"[图表] {lang} → {dest}")
    return skipped
