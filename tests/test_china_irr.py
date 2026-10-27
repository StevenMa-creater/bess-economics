import numpy as np
import pytest

from bess_econ.china_irr import presets
from bess_econ.china_irr.model import Assumptions, build_cashflow, evaluate, irr, npv, payback_years


def test_irr_matches_known_annuity():
    # 投 100，连续 5 年各收 25：IRR ≈ 7.93%
    cf = np.array([-100, 25, 25, 25, 25, 25], dtype=float)
    assert abs(irr(cf) - 0.0793) < 1e-3
    assert abs(npv(irr(cf), cf)) < 1e-4


def test_payback_linear_interpolation():
    cf = np.array([-100, 40, 40, 40], dtype=float)
    assert abs(payback_years(cf) - 2.5) < 1e-9


def test_capacity_price_formula_caps_at_one():
    a = Assumptions(duration_h=2, coal_capacity_price_yuan_per_kw_yr=165, net_load_peak_hours=4)
    assert a.capacity_price_yuan_per_kw_yr == pytest.approx(82.5)
    b = a.with_(duration_h=6)
    assert b.capacity_price_yuan_per_kw_yr == pytest.approx(165.0)


def test_cashflow_shape_and_capex_sign():
    a = presets.get("shandong", duration_h=2, power_mw=100)
    df = build_cashflow(a)
    assert len(df) == a.project_years + 1
    assert df.cf_project_pretax.iloc[0] == pytest.approx(-a.capex_wan)
    assert (df.revenue.iloc[1:] > 0).all()
    # 衰减：第 2 年放电量低于第 1 年
    assert df.discharged_mwh.iloc[2] < df.discharged_mwh.iloc[1]


def test_capacity_price_raises_irr():
    base = evaluate(presets.get("no_capacity_price", 2, 100)).irr_project_posttax
    with_cp = evaluate(presets.get("shandong", 2, 100)).irr_project_posttax
    assert with_cp > base + 0.03


def test_efficiency_monotonic():
    a = presets.get("shandong", 2, 100)
    r0 = evaluate(a).irr_project_posttax
    r1 = evaluate(a.with_(rte=a.rte + 0.02)).irr_project_posttax
    assert r1 > r0


def test_breakeven_revenue_hits_hurdle():
    from bess_econ.china_irr.model import breakeven_revenue
    a = presets.get("shandong", 2, 100)
    be = breakeven_revenue(a, hurdle=0.06)
    assert 1500 < be < 4000  # 万元，量级合理
