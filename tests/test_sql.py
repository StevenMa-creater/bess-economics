"""SQL 模块测试。

大部分用合成数据（不依赖真实电价，任何电脑都能跑）；
带 real_data 的两项需要 data/raw/ 里的 24 个 AEMO CSV，没有时自动跳过。
"""
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_fixture import synth, write_monthly  # noqa: E402
from bess_econ.dispatch import Battery, fixed_schedule_day, optimise_day  # noqa: E402
from bess_econ.nem import data as D  # noqa: E402
from bess_econ.nem import explore as X  # noqa: E402
from bess_econ.sql import db, runner  # noqa: E402

SQL_DIR = ROOT / "sql"
REAL_RAW = ROOT / "data" / "raw"
HAS_REAL = len(list(REAL_RAW.glob("PRICE_AND_DEMAND_*_QLD1.csv"))) == 24
real_data = pytest.mark.skipif(not HAS_REAL, reason="需要 data/raw/ 下的 24 个 AEMO 月度 CSV")


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    """合成电价（2025 年 1–3 月）+ 同期的真实回测结果，建一个小数据库。"""
    tmp = tmp_path_factory.mktemp("sqldb")
    raw = tmp / "raw"
    write_monthly(synth("QLD1", "2025-01-01", "2025-04-01", seed=1), raw, "QLD1")
    out = tmp / "outputs"
    (out / "nem").mkdir(parents=True)
    for k in ("perfect", "gbm", "persistence", "fixed"):
        d = pd.read_csv(ROOT / "outputs" / "nem" / f"daily_{k}.csv")
        d[d.trade_date < "2025-04-01"].to_csv(out / "nem" / f"daily_{k}.csv", index=False)
    shutil.copytree(ROOT / "outputs" / "scenarios", out / "scenarios", ignore=shutil.ignore_patterns("daily"))
    res = db.build(tmp / "test.sqlite", raw, out, log=lambda *a: None)
    conn = db.connect(tmp / "test.sqlite")
    yield dict(conn=conn, raw=raw, res=res)
    conn.close()


def test_build_rows_and_quality(fixture_db):
    res = fixture_db["res"]
    assert res["counts"]["fact_price"] == 90 * 288
    assert res["counts"]["dim_interval"] == 288
    assert res["counts"]["fact_dispatch_daily"] == 90 * 4
    assert res["quality_passed"], res["quality"].to_string()
    # data_quality 表里每一项都通过
    assert fixture_db["conn"].execute("SELECT MIN(passed) FROM data_quality").fetchone()[0] == 1


def test_monthly_avg_matches_pandas(fixture_db):
    sql = pd.read_sql_query("SELECT strftime('%Y-%m', trade_date) AS ym, AVG(rrp) AS avg_rrp "
                            "FROM fact_price GROUP BY ym ORDER BY ym", fixture_db["conn"]).set_index("ym").avg_rrp
    df = D.load(fixture_db["raw"], "QLD1")
    pdm = df.groupby(df.trade_date.dt.strftime("%Y-%m")).rrp.mean()
    assert np.allclose(sql.to_numpy(), pdm.loc[sql.index].to_numpy(), rtol=0, atol=1e-9)


def test_views_query_and_spread_matches_pandas(fixture_db):
    conn = fixture_db["conn"]
    assert conn.execute("SELECT COUNT(*) FROM v_price").fetchone()[0] == 90 * 288
    ms = pd.read_sql_query("SELECT * FROM v_monthly_summary ORDER BY year_month", conn)
    assert len(ms) == 3 and ms.gbm_capture.between(0, 1.2).all()
    v = pd.read_sql_query("SELECT trade_date, spread_2h FROM v_daily_spread ORDER BY trade_date", conn)
    P = D.to_matrix(D.load(fixture_db["raw"], "QLD1"))
    expected = X.daily_spreads(P).spread_2h.to_numpy()
    assert len(v) == 90 and np.allclose(v.spread_2h.to_numpy(), expected, atol=1e-9)


def test_all_query_levels_run(fixture_db):
    res = runner.run_levels(fixture_db["conn"], SQL_DIR, [1, 2, 3], None, log=lambda *a: None)
    names = [q.name for f in runner.LEVEL_FILES.values() for q in runner.parse(SQL_DIR / f)]
    assert set(names) == set(res) and len(names) >= 25
    # 每条查询的注释里都写了业务问题、知识点、结果解读
    for f in runner.LEVEL_FILES.values():
        for q in runner.parse(SQL_DIR / f):
            text = " ".join(q.comments)
            assert all(k in text for k in ("业务问题", "知识点", "结果解读")), q.name
    assert "SEARCH fact_price USING COVERING INDEX idx_price_rrp" in res["L3_13_explain_with_index"].detail.iloc[0]
    assert "SCAN fact_price" in res["L3_12_explain_no_index"].detail.iloc[0]


def test_sql_strategy_bounded_by_milp_and_fixed(fixture_db):
    """SQL 有序版在 MILP 的可行域内（MILP 起点设为 5% SOC 时），所以一定 ≤ MILP 完美预知；
    固定时段规则是它的一个候选组合，所以一定 ≥ 固定规则。逐日严格检查。"""
    conn = fixture_db["conn"]
    runner.run_file(conn, SQL_DIR / "03_advanced.sql", None, log=lambda *a: None)
    bt = pd.read_sql_query("SELECT * FROM bt_sql_strategy_daily ORDER BY trade_date", conn).set_index("trade_date")
    P = D.to_matrix(D.load(fixture_db["raw"], "QLD1"))
    b_empty_start = Battery(soc_init=0.05)
    for day in P.index[:12]:
        p = P.loc[day].to_numpy()
        sql_rev = bt.loc[day.strftime("%Y-%m-%d"), "ordered_revenue"]
        milp = optimise_day(p, b_empty_start).revenue
        fixed = fixed_schedule_day(p, Battery()).revenue
        assert sql_rev <= milp + 1e-3 * abs(milp) + 1.0, day
        assert sql_rev >= fixed - 1e-6, day
    assert (bt.naive_revenue >= bt.ordered_revenue - 1e-6).all()   # 无序版不受先后约束，只会更高


@real_data
def test_real_database(tmp_path):
    res = db.build(tmp_path / "real.sqlite", REAL_RAW, ROOT / "outputs", log=lambda *a: None)
    assert res["counts"]["fact_price"] == 210_528
    assert res["counts"]["fact_dispatch_daily"] == 365 * 4
    assert res["quality_passed"], res["quality"].to_string()
    conn = db.connect(tmp_path / "real.sqlite")
    try:
        runner.run_file(conn, SQL_DIR / "03_advanced.sql", None, log=lambda *a: None)
        ordered, naive = conn.execute("SELECT SUM(ordered_revenue), SUM(naive_revenue) FROM bt_sql_strategy_daily").fetchone()
        perfect, fixed = conn.execute("SELECT SUM(CASE WHEN strategy = 'perfect' THEN revenue END), "
                                      "SUM(CASE WHEN strategy = 'fixed' THEN revenue END) FROM fact_dispatch_daily").fetchone()
        assert fixed <= ordered <= naive <= perfect
    finally:
        conn.close()
