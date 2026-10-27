"""省份 / 场景预设。

所有数字都是 2026 年 10 月能公开查到的区间中值，用于演示和对比，不是任何一个真实项目的参数。
来源和"未核实"标注见 README「参数来源」一节。改参数请直接改这里，或在命令行用 --set 覆盖。
"""
from __future__ import annotations

from .model import Assumptions

# 通用成本假设（全国）
COMMON = dict(
    capex_system_yuan_per_wh=0.60,   # 2026H1 2h 系统中标均价 0.5993 元/Wh（CNESA）
    capex_bop_yuan_per_wh=0.30,      # 升压并网、土建、开发费，经验区间 0.25–0.40
    cycles_per_day=1.1,              # 2025 年全国新型储能平均利用小时 1195h；新项目按实际调度保守取 1.0–1.3
)

PRESETS: dict[str, Assumptions] = {
    # 山东：现货波动最大、负电价小时数最多；容量电价按 2026 年煤电容量电价基准 165 元/kW·年推算（未核实本省储能细则）
    "shandong": Assumptions(
        name="山东·独立储能",
        charge_price_yuan_per_kwh=0.18,     # 午间负电价/低价时段充电，年均含负电价时段
        discharge_price_yuan_per_kwh=0.46,  # 晚峰放电；山东价差日间 0.3–3 元/kWh 波动极大，年均按存量电站电能量收入反推
        coal_capacity_price_yuan_per_kw_yr=165.0,
        net_load_peak_hours=4.0,
        capacity_lease_yuan_per_kw_yr=0.0,  # 存量项目有租赁（约 50% 收入），新增独立储能取消强制配储后按 0
        ancillary_yuan_per_kw_yr=12.0,
        cycles_per_day=1.15,
        **{k: v for k, v in COMMON.items() if k != "cycles_per_day"},
    ),
    # 甘肃：容量电价基准 330 元/kW·年（已出台），弃风弃光多、充电便宜，但放电价格低
    "gansu": Assumptions(
        name="甘肃·独立储能",
        charge_price_yuan_per_kwh=0.10,
        discharge_price_yuan_per_kwh=0.31,
        coal_capacity_price_yuan_per_kw_yr=330.0,
        net_load_peak_hours=4.0,
        ancillary_yuan_per_kw_yr=15.0,
        cycles_per_day=1.1,
        **{k: v for k, v in COMMON.items() if k != "cycles_per_day"},
    ),
    # 广东：用户侧价差最大（珠三角 1.25 元/kWh），独立储能容量电价 200 元/kW·年为市场传闻，未核实
    "guangdong": Assumptions(
        name="广东·独立储能",
        charge_price_yuan_per_kwh=0.34,
        discharge_price_yuan_per_kwh=0.72,
        coal_capacity_price_yuan_per_kw_yr=200.0,
        net_load_peak_hours=4.0,
        ancillary_yuan_per_kw_yr=15.0,
        cycles_per_day=0.9,
        **{k: v for k, v in COMMON.items() if k != "cycles_per_day"},
    ),
    # 对照：没有容量电价、只靠套利的"136 号文之后、114 号文之前"状态
    "no_capacity_price": Assumptions(
        name="对照·无容量电价",
        charge_price_yuan_per_kwh=0.18,
        discharge_price_yuan_per_kwh=0.46,
        capacity_price_override=0.0,
        ancillary_yuan_per_kw_yr=12.0,
        cycles_per_day=1.15,
        **{k: v for k, v in COMMON.items() if k != "cycles_per_day"},
    ),
}


def get(name: str, duration_h: float | None = None, power_mw: float | None = None) -> Assumptions:
    a = PRESETS[name]
    kw = {}
    if duration_h is not None:
        kw["duration_h"] = duration_h
    if power_mw is not None:
        kw["power_mw"] = power_mw
    if kw:
        a = a.with_(**kw)
        a = a.with_(name=f"{a.name} {a.power_mw:.0f}MW/{a.energy_mwh:.0f}MWh")
    return a
