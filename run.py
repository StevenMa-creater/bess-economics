#!/usr/bin/env python3
"""bess-economics 命令行入口。

澳洲 NEM（真实数据，需先把 AEMO 月度 CSV 放进 data/raw/）：
  python run.py nem-urls --start 2024-01 --end 2026-01        # 打印要下载的 24 个链接
  python run.py nem                                            # 全流程，结果在 outputs/nem/
  python run.py nem --demo                                     # 用合成数据验证流程（结论无效）

情景实验（改一两个参数，和基准 2h / 1.5 循环对比；先跑过 nem）：
  python run.py scenario --hours 4
  python run.py scenario --max-cycles 1.0 --note "我发现……"

中国独立储能 IRR 模型：
  python run.py china evaluate --preset shandong --hours 2
  python run.py china compare
  python run.py china sensitivity --preset shandong --hours 2
  python run.py china all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


# --------------------------------------------------------------------------- NEM
def cmd_nem_urls(a):
    from bess_econ.nem.data import monthly_urls
    for u in monthly_urls(a.region, a.start, a.end):
        print(u)


def cmd_nem(a):
    from bess_econ import pipeline
    if a.demo:
        fx = ROOT / "data" / "fixture"
        if not any(fx.glob("*.csv")):
            subprocess.run([sys.executable, str(ROOT / "scripts" / "make_fixture.py"), "--out", str(fx)], check=True)
        pipeline.run(fx, ROOT / "outputs" / "nem_demo", a.region, a.test_start, synthetic=True, mc_runs=a.mc)
    else:
        pipeline.run(ROOT / "data" / "raw", ROOT / "outputs" / "nem", a.region, a.test_start, synthetic=False, mc_runs=a.mc)


def cmd_scenario(a):
    from bess_econ import scenario
    note = a.note
    if a.note_file and Path(a.note_file).exists():
        note = Path(a.note_file).read_text(encoding="utf-8-sig").strip()
    scenario.run(ROOT, hours=a.hours, max_cycles=a.max_cycles, rte=a.rte, capex=a.capex, name=a.name,
                 note=note, region=a.region, test_start=a.test_start, mc_runs=a.mc)


# --------------------------------------------------------------------------- China
def _china(a):
    from bess_econ.china_irr import presets
    from bess_econ.china_irr.model import build_cashflow, evaluate
    from bess_econ.china_irr.sensitivity import (efficiency_value, plot_compare, plot_tornado,
                                                 province_compare, tornado)
    out = ROOT / "outputs" / "china"
    out.mkdir(parents=True, exist_ok=True)
    pct = lambda x: "n/a" if pd.isna(x) else f"{x*100:.2f}%"

    def build(preset, hours, mw, sets):
        b = presets.get(preset, duration_h=hours, power_mw=mw)
        kv = {}
        for it in sets or []:
            k, v = it.split("=", 1)
            kv[k] = None if v.lower() in ("none", "null") else (v.lower() == "true" if v.lower() in ("true", "false") else float(v))
        return b.with_(**kv) if kv else b

    def evaluate_one(preset, hours, mw=100.0, sets=None, hurdle=0.06):
        b = build(preset, hours, mw, sets)
        r = evaluate(b, hurdle=hurdle)
        build_cashflow(b).round(1).to_csv(out / f"cashflow_{preset}_{b.duration_h:.0f}h.csv", index=False)
        ev = efficiency_value(b)
        print(f"\n=== {b.name} ===")
        print(f"初始投资 {r.capex_wan:,.0f} 万元 | 容量电价 {r.capacity_price:.1f} 元/kW·年 | 第 1 年收入 {r.revenue_year1_wan:,.0f} 万元")
        print(f"IRR 全投资税后 {pct(r.irr_project_posttax)} | 资本金 {pct(r.irr_equity)} | 回收期 {r.payback_project_years:.1f} 年 | LCOS {r.lcos_yuan_per_kwh:.3f} 元/kWh")
        print(f"IRR {hurdle*100:.0f}% 所需年收入 {r.breakeven_revenue_wan:,.0f} 万元 | 效率 +1pp：全生命周期 +{ev['delta_lifetime_revenue_wan']:,.0f} 万元")
        return b, r, ev

    if a.china_cmd == "evaluate":
        evaluate_one(a.preset, a.hours, a.mw, a.set, a.hurdle)
    elif a.china_cmd == "compare":
        df = province_compare(presets.PRESETS, power_mw=a.mw)
        df.round(4).to_csv(out / "province_compare.csv", index=False)
        plot_compare(df, out / "province_compare.png")
        print(df[["name", "duration_h", "capacity_price", "revenue_y1_wan", "irr_posttax", "irr_equity", "payback"]].to_string(index=False))
    elif a.china_cmd == "sensitivity":
        b = build(a.preset, a.hours, a.mw, a.set)
        df = tornado(b)
        df.round(4).to_csv(out / f"tornado_{a.preset}_{b.duration_h:.0f}h.csv", index=False)
        plot_tornado(df, b.name, out / f"tornado_{a.preset}_{b.duration_h:.0f}h.png")
        print(df.to_string(index=False))
    elif a.china_cmd == "all":
        summary = {}
        for p in presets.PRESETS:
            for h in (2.0, 4.0):
                b, r, ev = evaluate_one(p, h)
                summary[f"{p}_{h:.0f}h"] = {**r.as_row(), "efficiency_plus_1pp": ev}
        df = province_compare(presets.PRESETS)
        df.round(4).to_csv(out / "province_compare.csv", index=False)
        plot_compare(df, out / "province_compare.png")
        for h in (2.0, 4.0):
            b = presets.get("shandong", duration_h=h, power_mw=100.0)
            plot_tornado(tornado(b), b.name, out / f"tornado_shandong_{h:.0f}h.png")
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=float), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="bess-economics：储能电站收益、调度与投资回报")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("nem-urls"); sp.add_argument("--region", default="QLD1")
    sp.add_argument("--start", default="2024-01"); sp.add_argument("--end", default="2026-01"); sp.set_defaults(fn=cmd_nem_urls)

    sp = sub.add_parser("nem"); sp.add_argument("--region", default="QLD1"); sp.add_argument("--test-start", default=None)
    sp.add_argument("--demo", action="store_true"); sp.add_argument("--mc", type=int, default=5000); sp.set_defaults(fn=cmd_nem)

    sp = sub.add_parser("scenario", help="情景实验：改电池参数，和基准对比收益与 IRR")
    sp.add_argument("--hours", type=float, default=2.0, help="储能时长（小时），基准 2")
    sp.add_argument("--max-cycles", type=lambda v: None if v.lower() == "none" else float(v), default=1.5,
                    help="每日循环上限，基准 1.5；none 表示不限")
    sp.add_argument("--rte", type=float, default=0.86, help="往返效率，基准 0.86")
    sp.add_argument("--capex", type=float, nargs=3, metavar=("LOW", "MODE", "HIGH"), default=None,
                    help="投资成本 A$/kWh；不填时按时长自动估算")
    sp.add_argument("--name", default=None, help="结果文件名，不填自动生成")
    sp.add_argument("--note", default="", help="你的一句话发现，会写进 README")
    sp.add_argument("--note-file", default=None, help="从文本文件读取发现（UTF-8）")
    sp.add_argument("--region", default="QLD1"); sp.add_argument("--test-start", default=None)
    sp.add_argument("--mc", type=int, default=5000); sp.set_defaults(fn=cmd_scenario)

    sp = sub.add_parser("china"); csub = sp.add_subparsers(dest="china_cmd", required=True)
    for name in ("evaluate", "sensitivity"):
        c = csub.add_parser(name)
        c.add_argument("--preset", default="shandong"); c.add_argument("--hours", type=float, default=2.0)
        c.add_argument("--mw", type=float, default=100.0); c.add_argument("--set", action="append", metavar="PARAM=VALUE")
        c.add_argument("--hurdle", type=float, default=0.06)
    c = csub.add_parser("compare"); c.add_argument("--mw", type=float, default=100.0)
    csub.add_parser("all")
    sp.set_defaults(fn=_china)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
