"""根据 outputs/nem/summary.json 和 outputs/china/summary.json 生成 README 的结论区块。

同时更新两份 README：README.md（英文，默认）和 README.zh-CN.md（中文）。
情景实验的"发现"：中文读 note，英文读 note_en（没有 note_en 时英文版不列这一条）。

    python scripts/update_readme.py            # 真实数据
    python scripts/update_readme.py --demo     # 用合成数据结果预览排版（会标注"非真实数据"）
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"


def k(x):  # A$ → 千
    return f"{x/1e3:,.0f}"


def pct(x, d=0):
    return f"{x*100:.{d}f}%"


def nem_block(s: dict, img: str) -> str:
    import pandas as pd
    perf = pd.read_csv(ROOT / img / "daily_perfect.csv", index_col=0, parse_dates=True).revenue
    top = perf.sort_values(ascending=False)
    top5_share = top.head(5).sum() / perf.sum()
    top_day = top.index[0].strftime("%Y-%m-%d")
    st = s["strategies"]
    m = s["market"]
    mc = s["montecarlo"]
    fm = s["forecast_metrics"]
    deg = sorted(s["degradation"], key=lambda r: -r["net_of_true_degradation_per_mw_year"])[0]
    deg0 = [r for r in s["degradation"] if r["delta"] == 0][0]
    dur = {int(r["duration_h"]): r for r in s["durations"]}
    warn = "" if s["data_source"] == "AEMO 公开数据" else "\n> ⚠️ 以下为**合成测试数据**的结果，仅用于展示排版，不是真实结论。\n"
    lines = [f"### 澳洲 NEM · {s['region']} · {s['test_start'][:4]} 年回测（{s['test_days']} 天，5 分钟电价）", warn,
             "| 策略 | 年套利收入（A$ 千/MW） | 收益捕获率 | 年等效循环 |", "|---|---|---|---|"]
    for key in ("perfect", "gbm", "persistence", "fixed"):
        r = st[key]
        lines.append(f"| {r['name']} | {k(r['revenue_aud_per_mw_year'])} | {pct(r['capture_rate'])} | {r['cycles_per_year']:.0f} |")
    lines += ["", f"![策略对比]({img}/03_strategy_capture.png)", "",
              "**结论**", "",
              f"1. **调度能力值多少钱**：同一块 100 MW / 200 MWh 电池，GBM 预测 + MILP 优化拿到完美预知上限的 **{pct(st['gbm']['capture_rate'])}**，"
              f"比\"照抄昨天电价\"高 {pct(st['gbm']['capture_rate'] - st['persistence']['capture_rate'])}（百分点），"
              f"比固定时段规则高 {pct(st['gbm']['capture_rate'] - st['fixed']['capture_rate'])}。"
              f"次日电价预测 MAE {fm['gbm']['mae']:.0f} A$/MWh（基准 {fm['persistence']['mae']:.0f}），日内排序相关性 {fm['gbm']['intraday_rank_corr']:.2f}（基准 {fm['persistence']['intraday_rank_corr']:.2f}）。"
              + ("**反直觉的一点**：用昨天电价做优化（" + pct(st['persistence']['capture_rate']) + "）反而不如固定时段规则（" + pct(st['fixed']['capture_rate'])
                 + "），因为优化器会把昨天的随机波动当成信号、过度交易；预测质量不够时，\"优化\"可能比不优化更差。"
                 if st['persistence']['capture_rate'] < st['fixed']['capture_rate'] else ""),
              f"2. **收益高度集中**：全年收益最高的 5 天贡献了完美预知收入的 **{pct(top5_share)}**，最高的一天是 {top_day}"
              + ("（与 Modo Energy 报告的\"1 月 22 日一天占昆州电池当月收入 57%\"一致，可作为模型的外部验证）" if top_day == "2025-01-22" else "")
              + "。能否在极端价格日满电待命，比日常的小价差更重要。",
              f"3. **市场形态**：负电价占全年 **{pct(m['negative_share'])}** 的时段（约 {m['negative_hours_per_year']:,.0f} 小时），"
              f"每日 2 小时价差中位数 **A${s['median_daily_spread_2h']:,.0f}/MWh**；午间光伏把电价压到负值、傍晚形成高峰，这是储能套利的来源。",
              f"4. **多循环不等于多赚钱**：按真实衰减成本 30 A$/MWh 计，调度时计入 δ = {deg['delta']:.0f} A$/MWh 时净收益最高"
              f"（{k(deg['net_of_true_degradation_per_mw_year'])} 千 A$/MW/年，{deg['cycles_per_year']:.0f} 次/年），"
              f"比不计衰减（δ = 0，{k(deg0['net_of_true_degradation_per_mw_year'])} 千、{deg0['cycles_per_year']:.0f} 次/年）多 {pct(deg['net_of_true_degradation_per_mw_year']/deg0['net_of_true_degradation_per_mw_year']-1)}，循环次数减半。",
              f"5. **时长**：1h / 2h / 4h 每 MW 年收入 {k(dur[1]['revenue_aud_per_mw_year'])} / {k(dur[2]['revenue_aud_per_mw_year'])} / {k(dur[4]['revenue_aud_per_mw_year'])} 千 A$，"
              f"每 MWh 收入递减（{k(dur[1]['revenue_aud_per_mwh_year'])} → {k(dur[4]['revenue_aud_per_mwh_year'])} 千 A$）。",
              f"6. **只靠能量套利能不能投**：蒙特卡洛 {mc['n']:,} 次，项目 IRR P10 / P50 / P90 = **{pct(mc['irr_p10'],1)} / {pct(mc['irr_p50'],1)} / {pct(mc['irr_p90'],1)}**，"
              f"超过 8% 门槛的概率 {pct(mc['prob_irr_above_hurdle'])}；达到 7% IRR 需要第 1 年约 **A${k(mc['breakeven_revenue_aud_per_mw_year'])} 千/MW** 收入。"
              f"GBM 策略实际可实现约 A${k(st['gbm']['revenue_aud_per_mw_year'])} 千/MW，略低于这条线，所以只靠能量套利不足以支撑投资，需要 FCAS 或长期容量合同补足。"
              f"作为对照，Modo Energy 统计 2024 年 NEM 电池平均总收入（含 FCAS）为 A$149 千/MW/年。",
              "",
              f"<details><summary>更多图表</summary>\n\n![鸭子曲线]({img}/01_duck_curve.png)\n![价差]({img}/02_daily_spread.png)\n"
              f"![累计收入]({img}/04_cumulative_revenue.png)\n![调度示例]({img}/05_example_day.png)\n"
              f"![衰减权衡]({img}/06_degradation_tradeoff.png)\n![时长]({img}/07_duration.png)\n![IRR 分布]({img}/08_irr_distribution.png)\n\n</details>", ""]
    return "\n".join(lines)


def scenario_block(files: list[Path]) -> str:
    """outputs/scenarios/*.json → 情景实验表（python run.py scenario 生成）。"""
    res = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    order = {"4h": 0, "cycles_1.0": 1, "4h_cycles_1.0": 2}
    res.sort(key=lambda r: (order.get(r["name"], 99), r["name"]))

    def row(name, m):
        g = m["gbm"]
        return (f"| {name} | {m['perfect']['revenue_k_per_mw']:.0f} | {g['revenue_k_per_mw']:.0f} | {pct(g['capture_rate'])} | "
                f"{g['cycles_per_year']:.0f} | {g['net_k_per_mw']:.0f} | {m['capex_mid_aud_m']:.0f} | "
                f"{pct(m['irr']['gross_p50'],1)} | {pct(m['irr']['net_p50'],1)} |")

    lines = ["### 情景实验：只改一个参数，结论怎么变", "",
             "同一年真实电价、同一个 GBM 预测，只改电池参数；投资成本按\"功率部分 560 A$/kW + 电池部分 240 A$/kWh\"两段式估算"
             "（2h 众数 520、4h 众数 380 A$/kWh）。复现：`python run.py scenario --hours 4`。", "",
             "| 情景（100 MW） | 完美预知 | GBM 收入 | 捕获率 | 年循环 | 扣衰减净收入 | 投资（百万 A$） | IRR P50（毛收入） | IRR P50（全寿命） |",
             "|---|---|---|---|---|---|---|---|---|",
             row("基准：2h / 1.5 循环", res[0]["baseline"])]
    lines += [row(r["label"].replace(" / RTE 86%", ""), r["scenario"]) for r in res]
    lines += ["", "收入单位：千 A$/MW/年。扣衰减净收入 = 收入 − 每放 1 MWh × 30 A$ 电芯损耗。IRR 毛收入口径与上文蒙特卡洛一致（容量每年固定衰减 2%，多循环不计成本）；全寿命口径按每 MWh 放电计提 30 A$ 补充电芯、容量保持，循环成本随放电量增加，比较循环上限时应看这一列。", ""]
    notes = [r for r in res if r.get("note")]
    if notes:
        lines += ["**我的发现**", ""] + [f"- **{r['label'].replace(' / RTE 86%', '')}**：{r['note']}" for r in notes] + [""]
    return "\n".join(lines)


def china_block(s: dict) -> str:
    rows = [("对照：无容量电价", "no_capacity_price_2h"), ("山东 2h", "shandong_2h"), ("山东 4h", "shandong_4h"),
            ("甘肃 2h", "gansu_2h"), ("广东 2h", "guangdong_2h")]
    lines = ["### 中国 · 独立储能 15 年现金流模型（114 号文容量电价）", "",
             "| 场景（100 MW） | 容量电价（元/kW·年） | 第 1 年收入（万元） | 全投资税后 IRR | 资本金 IRR | 回收期 |", "|---|---|---|---|---|---|"]
    for name, key in rows:
        r = s[key]
        lines.append(f"| {name} | {r['capacity_price']:.1f} | {r['revenue_year1_wan']:,.0f} | {pct(r['irr_project_posttax'],1)} | "
                     f"{pct(r['irr_equity'],1)} | {r['payback_project_years']:.1f} 年 |")
    lines += ["", "![各省对比](outputs/china/province_compare.png)", "",
              f"没有容量电价时只靠套利 IRR 约 {pct(s['no_capacity_price_2h']['irr_project_posttax'],1)}，"
              f"山东 2h 加容量电价后 {pct(s['shandong_2h']['irr_project_posttax'],1)}；龙卷风敏感性分析显示放电价差、循环次数、充电价格三个交易参数对 IRR 的影响最大，设备投资排第四。",
              "", "![山东敏感性](outputs/china/tornado_shandong_2h.png)", "",
              "> 容量电价口径：甘肃按已出台的煤电容量电价基准 330 元/kW·年折算；山东按全国煤电容量电价基准 165 元/kW·年推算，"
              "广东 200 元/kW·年为市场传闻值，两省储能容量电价细则出台后需更新（见 `bess_econ/china_irr/presets.py`）。", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- English

STRATEGY_EN = {"perfect": "Perfect foresight (upper bound)", "gbm": "GBM forecast + optimiser",
               "persistence": "Yesterday's prices + optimiser", "fixed": "Fixed-schedule rule"}


def nem_block_en(s: dict, img: str) -> str:
    import pandas as pd
    perf = pd.read_csv(ROOT / img / "daily_perfect.csv", index_col=0, parse_dates=True).revenue
    top = perf.sort_values(ascending=False)
    top5_share = top.head(5).sum() / perf.sum()
    top_day = top.index[0].strftime("%Y-%m-%d")
    st, m, mc, fm = s["strategies"], s["market"], s["montecarlo"], s["forecast_metrics"]
    deg = sorted(s["degradation"], key=lambda r: -r["net_of_true_degradation_per_mw_year"])[0]
    deg0 = [r for r in s["degradation"] if r["delta"] == 0][0]
    dur = {int(r["duration_h"]): r for r in s["durations"]}
    warn = "" if s["data_source"] == "AEMO 公开数据" else "\n> ⚠️ Results below come from **synthetic test data** and only preview the layout. They are not real findings.\n"
    g, pe, fx = st["gbm"], st["persistence"], st["fixed"]
    lines = [f"### Australia NEM · {s['region']} · {s['test_start'][:4]} backtest ({s['test_days']} days, 5-minute prices)", warn,
             "| Strategy | Arbitrage revenue (A$k/MW/yr) | Capture rate | Equivalent cycles/yr |", "|---|---|---|---|"]
    for key in ("perfect", "gbm", "persistence", "fixed"):
        r = st[key]
        lines.append(f"| {STRATEGY_EN[key]} | {k(r['revenue_aud_per_mw_year'])} | {pct(r['capture_rate'])} | {r['cycles_per_year']:.0f} |")
    counter = ""
    if pe["capture_rate"] < fx["capture_rate"]:
        counter = (f" **Counter-intuitive result:** optimising against yesterday's prices ({pct(pe['capture_rate'])}) does worse than a simple "
                   f"fixed-schedule rule ({pct(fx['capture_rate'])}). The optimiser treats yesterday's noise as signal and over-trades; "
                   "with a weak forecast, optimising can be worse than not optimising.")
    jan22 = (" — consistent with Modo Energy's finding that 22 January alone delivered 57% of Queensland battery revenue that month, "
             "an external check on the model" if top_day == "2025-01-22" else "")
    lines += ["", f"![Strategy comparison]({img}/03_strategy_capture.png)", "", "**Key findings**", "",
              f"1. **What dispatch skill is worth.** For the same 100 MW / 200 MWh battery, a GBM forecast feeding the MILP captures "
              f"**{pct(g['capture_rate'])}** of the perfect-foresight revenue — {(g['capture_rate'] - pe['capture_rate'])*100:.0f} percentage points above "
              f"re-using yesterday's prices and {(g['capture_rate'] - fx['capture_rate'])*100:.0f} points above a fixed charge/discharge schedule. "
              f"Day-ahead forecast MAE is {fm['gbm']['mae']:.0f} A$/MWh (baseline {fm['persistence']['mae']:.0f}); intraday rank correlation "
              f"{fm['gbm']['intraday_rank_corr']:.2f} (baseline {fm['persistence']['intraday_rank_corr']:.2f})." + counter,
              f"2. **Revenue is concentrated in a few days.** The top 5 days deliver **{pct(top5_share)}** of annual perfect-foresight revenue; "
              f"the best day is {top_day}{jan22}. Being full and available on extreme-price days matters more than everyday small spreads.",
              f"3. **Market shape.** Prices are negative in **{pct(m['negative_share'])}** of intervals (about {m['negative_hours_per_year']:,.0f} hours a year); "
              f"the median daily 2-hour spread is **A${s['median_daily_spread_2h']:,.0f}/MWh**. Midday solar pushes prices below zero and the evening "
              "ramp sets the peak — this duck curve is where storage earns its money.",
              f"4. **More cycling is not more profit.** With a true degradation cost of A$30/MWh discharged, net revenue peaks when the dispatch "
              f"model includes a wear cost of δ = {deg['delta']:.0f} A$/MWh ({k(deg['net_of_true_degradation_per_mw_year'])}k A$/MW/yr at "
              f"{deg['cycles_per_year']:.0f} cycles/yr) — {pct(deg['net_of_true_degradation_per_mw_year']/deg0['net_of_true_degradation_per_mw_year']-1)} "
              f"more than ignoring wear (δ = 0: {k(deg0['net_of_true_degradation_per_mw_year'])}k at {deg0['cycles_per_year']:.0f} cycles/yr), with half the cycles.",
              f"5. **Duration.** Revenue per MW for 1h / 2h / 4h is {k(dur[1]['revenue_aud_per_mw_year'])} / {k(dur[2]['revenue_aud_per_mw_year'])} / "
              f"{k(dur[4]['revenue_aud_per_mw_year'])}k A$/yr; revenue per MWh falls from {k(dur[1]['revenue_aud_per_mwh_year'])}k to "
              f"{k(dur[4]['revenue_aud_per_mwh_year'])}k A$.",
              f"6. **Is merchant arbitrage alone investable?** Across {mc['n']:,} Monte Carlo runs, project IRR P10 / P50 / P90 = "
              f"**{pct(mc['irr_p10'],1)} / {pct(mc['irr_p50'],1)} / {pct(mc['irr_p90'],1)}**, with a {pct(mc['prob_irr_above_hurdle'])} chance of "
              f"clearing an 8% hurdle. A 7% IRR needs about **A${k(mc['breakeven_revenue_aud_per_mw_year'])}k/MW** in year 1, while the GBM strategy "
              f"realises about A${k(g['revenue_aud_per_mw_year'])}k/MW. Energy arbitrage alone falls just short; the project needs FCAS revenue or a "
              "long-term capacity/tolling contract. For reference, Modo Energy reports an average NEM battery revenue of A$149k/MW/yr in 2024 (including FCAS).",
              "",
              f"<details><summary>More charts</summary>\n\n![Duck curve]({img}/01_duck_curve.png)\n![Daily spread]({img}/02_daily_spread.png)\n"
              f"![Cumulative revenue]({img}/04_cumulative_revenue.png)\n![Example day]({img}/05_example_day.png)\n"
              f"![Degradation trade-off]({img}/06_degradation_tradeoff.png)\n![Duration]({img}/07_duration.png)\n"
              f"![IRR distribution]({img}/08_irr_distribution.png)\n\n</details>", ""]
    return "\n".join(lines)


def _label_en(label: str) -> str:
    return label.replace(" / RTE 86%", "").replace("循环上限", "cycle cap")


def scenario_block_en(files: list[Path]) -> str:
    res = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    order = {"4h": 0, "cycles_1.0": 1, "4h_cycles_1.0": 2}
    res.sort(key=lambda r: (order.get(r["name"], 99), r["name"]))

    def row(name, m):
        g = m["gbm"]
        return (f"| {name} | {m['perfect']['revenue_k_per_mw']:.0f} | {g['revenue_k_per_mw']:.0f} | {pct(g['capture_rate'])} | "
                f"{g['cycles_per_year']:.0f} | {g['net_k_per_mw']:.0f} | {m['capex_mid_aud_m']:.0f} | "
                f"{pct(m['irr']['gross_p50'],1)} | {pct(m['irr']['net_p50'],1)} |")

    lines = ["### Scenario experiments: change one parameter, see what moves", "",
             "Same year of real prices and the same GBM forecast; only the battery parameters change. Capex uses a two-part estimate "
             "(A$560/kW power + A$240/kWh energy; mode A$520/kWh for 2h and A$380/kWh for 4h). Reproduce with `python run.py scenario --hours 4`.", "",
             "| Scenario (100 MW) | Perfect foresight | GBM revenue | Capture | Cycles/yr | Net of cell wear | Capex (A$m) | IRR P50 (gross) | IRR P50 (lifetime) |",
             "|---|---|---|---|---|---|---|---|---|",
             row("Base: 2h / 1.5 cycles", res[0]["baseline"])]
    lines += [row(_label_en(r["label"]), r["scenario"]) for r in res]
    lines += ["", "Revenue in A$k/MW/yr. Net of cell wear = revenue − A$30 per MWh discharged. Gross IRR matches the Monte Carlo above "
              "(capacity fades a fixed 2% a year, extra cycling is free); lifetime IRR sets aside A$30 per MWh discharged to replace cells and keep capacity, "
              "so cycling has a cost — use this column when comparing cycle caps.", ""]
    notes = [r for r in res if r.get("note_en")]
    if notes:
        lines += ["**Findings**", ""] + [f"- **{_label_en(r['label'])}**: {r['note_en']}" for r in notes] + [""]
    return "\n".join(lines)


CHINA_EN = [("Reference: no capacity price", "no_capacity_price_2h"), ("Shandong 2h", "shandong_2h"), ("Shandong 4h", "shandong_4h"),
            ("Gansu 2h", "gansu_2h"), ("Guangdong 2h", "guangdong_2h")]


def china_block_en(s: dict) -> str:
    lines = ["### China · 15-year cash-flow model for independent storage (Doc. 114 capacity price)", "",
             "| Case (100 MW) | Capacity price (CNY/kW·yr) | Year-1 revenue (CNY m) | Project IRR (post-tax) | Equity IRR | Payback |",
             "|---|---|---|---|---|---|"]
    for name, key in CHINA_EN:
        r = s[key]
        lines.append(f"| {name} | {r['capacity_price']:.1f} | {r['revenue_year1_wan']/100:,.1f} | {pct(r['irr_project_posttax'],1)} | "
                     f"{pct(r['irr_equity'],1)} | {r['payback_project_years']:.1f} yrs |")
    lines += ["", "![Province comparison](outputs/china/province_compare.png)", "",
              f"With arbitrage alone, project IRR is about {pct(s['no_capacity_price_2h']['irr_project_posttax'],1)}; adding the capacity price lifts "
              f"a 2h project in Shandong to {pct(s['shandong_2h']['irr_project_posttax'],1)}. The tornado chart shows that three trading parameters — "
              "discharge spread, cycles per day and charging price — move IRR more than equipment capex, which ranks fourth.",
              "", "![Shandong sensitivity](outputs/china/tornado_shandong_2h.png)", "",
              "> Capacity-price basis: Gansu uses its published coal capacity-price benchmark of CNY 330/kW·yr. Shandong is estimated from the national "
              "coal benchmark of CNY 165/kW·yr, and Guangdong's CNY 200/kW·yr is an unconfirmed market figure; both should be updated once the "
              "provinces publish storage-specific rules (see `bess_econ/china_irr/presets.py`).", ""]
    return "\n".join(lines)


def _write(readme: Path, parts: list[str]) -> None:
    text = readme.read_text(encoding="utf-8")
    block = f"{START}\n" + "\n".join(parts) + f"\n{END}"
    text = re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    readme.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    nem_dir = "outputs/nem_demo" if a.demo else "outputs/nem"
    nem_json = ROOT / nem_dir / "summary.json"
    scen = sorted((ROOT / "outputs/scenarios").glob("*.json")) if not a.demo else []
    china_json = ROOT / "outputs/china/summary.json"
    nem = json.loads(nem_json.read_text(encoding="utf-8")) if nem_json.exists() else None
    china = json.loads(china_json.read_text(encoding="utf-8")) if china_json.exists() else None

    zh = [nem_block(nem, nem_dir) if nem else "> 澳洲 NEM 结果尚未生成：按「用法」下载数据后运行 `python run.py nem`。\n"]
    en = [nem_block_en(nem, nem_dir) if nem else "> NEM results not generated yet: download the data (see Usage) and run `python run.py nem`.\n"]
    if scen:
        zh.append(scenario_block(scen))
        en.append(scenario_block_en(scen))
    if china:
        zh.append(china_block(china))
        en.append(china_block_en(china))
    _write(ROOT / "README.zh-CN.md", zh)
    _write(ROOT / "README.md", en)
    print("README.md（英文）和 README.zh-CN.md（中文）的结论区块已更新")


if __name__ == "__main__":
    main()
