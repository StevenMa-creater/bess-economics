import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_fixture import synth, write_monthly  # noqa: E402
from bess_econ.nem import data as D  # noqa: E402
from bess_econ.nem import explore as X  # noqa: E402
from bess_econ.dispatch import Battery, optimise_day, fixed_schedule_day  # noqa: E402
from bess_econ import forecast as F  # noqa: E402
from bess_econ import montecarlo as MC  # noqa: E402


@pytest.fixture(scope="module")
def raw_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("raw")
    write_monthly(synth("QLD1", "2025-01-01", "2025-04-01", seed=1), d, "QLD1")
    return d


@pytest.fixture(scope="module")
def df(raw_dir):
    return D.load(raw_dir, "QLD1")


def test_loader_counts_and_interval_convention(df):
    # 90 天 × 288 区间；第一行区间结束 00:05、开始 00:00
    assert len(df) == 90 * 288
    first = df.iloc[0]
    assert first.settlementdate == pd.Timestamp("2025-01-01 00:05")
    assert first.interval_start == pd.Timestamp("2025-01-01 00:00")
    assert first.slot == 0
    # 最后一个区间（结束于次日 00:00）应归属前一天
    last = df.iloc[-1]
    assert last.settlementdate == pd.Timestamp("2025-04-01 00:00")
    assert last.trade_date == pd.Timestamp("2025-03-31")
    assert last.slot == 287


def test_loader_handles_gaps_and_duplicates(raw_dir, tmp_path):
    files = sorted(raw_dir.glob("*.csv"))
    f = pd.read_csv(files[0])
    f = pd.concat([f.drop(index=[100, 101, 102]), f.iloc[[10]]])  # 缺 3 个区间 + 1 个重复
    f.to_csv(tmp_path / files[0].name, index=False)
    log = []
    out = D.complete_days(D.clean(D.load_raw([tmp_path / files[0].name], "QLD1"), log), log)
    D.validate(out)
    assert out.trade_date.nunique() == 31
    assert "补齐 3" in log[-1]


def test_nemosis_format_intervention_filter():
    raw = pd.DataFrame({"SETTLEMENTDATE": ["2025/01/01 00:05:00"] * 2, "REGIONID": ["QLD1"] * 2,
                        "RRP": [50.0, 9999.0], "INTERVENTION": [0, 1]})
    n = D._normalise(raw)
    assert len(n) == 1 and n.rrp.iloc[0] == 50.0


def _flat_peak_prices():
    p = np.full(288, 50.0)
    p[120:144] = -20.0    # 10:00–12:00 低价
    p[216:240] = 300.0    # 18:00–20:00 高价
    return p


def test_lp_buys_low_sells_high_and_respects_physics():
    b = Battery(max_cycles_per_day=1.0)
    p = _flat_peak_prices()
    r = optimise_day(p, b)
    assert r.charge[120:144].sum() > 0 and r.discharge[216:240].sum() > 0
    assert r.revenue > 0
    assert np.all(r.soc >= b.soc_min * b.energy_mwh - 1e-6) and np.all(r.soc <= b.soc_max * b.energy_mwh + 1e-6)
    assert abs(r.soc[-1] - b.soc_init * b.energy_mwh) < 1e-6
    assert r.discharge_mwh <= b.usable_mwh + 1e-6
    # 能量守恒：放电量 ≈ 充电量 × RTE（日末 SOC 回到初值）
    assert abs(r.discharge_mwh - r.charge_mwh * b.rte) < 1e-4
    assert not np.any((r.charge > 1e-6) & (r.discharge > 1e-6))


def test_flat_prices_give_no_trade():
    r = optimise_day(np.full(288, 80.0), Battery())
    assert r.discharge_mwh < 1e-6 and abs(r.revenue) < 1e-6


def test_perfect_foresight_beats_any_plan(df):
    P = D.to_matrix(df)
    b = Battery()
    for i in (5, 40, 80):
        a = P.iloc[i].to_numpy()
        perfect = optimise_day(a, b).revenue
        noisy = optimise_day(a + np.random.default_rng(i).normal(0, 60, 288), b, a).revenue
        fixed = fixed_schedule_day(a, b).revenue
        assert perfect >= noisy - 1e-6 and perfect >= fixed - 1e-6


def test_degradation_cost_reduces_cycling(df):
    a = D.to_matrix(df).iloc[30].to_numpy()
    b = Battery(max_cycles_per_day=None)
    c0 = optimise_day(a, b).discharge_mwh
    c1 = optimise_day(a, b.with_(degradation_cost=80.0)).discharge_mwh
    assert c1 <= c0 + 1e-6


def test_forecast_features_have_no_leakage(df):
    P = D.to_matrix(df)
    X = F.features(P)
    day = P.index[20]
    row = X.loc[(day, 100)]
    assert row.lag1 == pytest.approx(P.loc[P.index[19], 100])
    assert row.lag7 == pytest.approx(P.loc[P.index[13], 100])
    assert row.y == pytest.approx(P.loc[day, 100])


def test_spread_metric():
    p = _flat_peak_prices()
    assert X.top_bottom_spread(p, 2) == pytest.approx(320.0)


def test_montecarlo_runs_and_breakeven_positive():
    rev = pd.Series(np.full(365, 400000.0 / 365 * 100))  # 年收入 A$400k/MW
    a = MC.AUAssumptions()
    sim = MC.simulate(rev, a, n=200)
    s = MC.summarise(sim, a)
    assert 0 <= s["prob_irr_above_hurdle"] <= 1
    assert MC.breakeven_revenue_per_mw(a) > 0


def test_scenario_capex_matches_baseline_at_2h_and_falls_with_duration():
    from bess_econ.scenario import BASE_CAPEX, capex_for_duration
    assert capex_for_duration(2) == BASE_CAPEX
    one, four = capex_for_duration(1), capex_for_duration(4)
    assert one[1] > BASE_CAPEX[1] > four[1]          # 每 kWh 成本随时长下降
    assert four[1] * 4 > BASE_CAPEX[1] * 2           # 但每 kW 总投资随时长上升
