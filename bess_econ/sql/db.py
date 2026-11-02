"""建库：把清洗后的 AEMO 电价、回测结果、情景实验写进 SQLite，并用 SQL 做数据质量检查。

只用 Python 自带的 sqlite3，不需要安装数据库软件。数据库就是一个文件（默认 data/bess.sqlite），
可以用 DB Browser for SQLite 之类的免费工具打开查看。

    python run.py sql build
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..nem import data as D

SCHEMA = Path(__file__).with_name("schema.sql")
STRATEGIES = {  # strategy: (中文名, 英文名, 是否用预测, 说明)
    "perfect": ("完美预知（上限）", "Perfect foresight", 0, "按当天真实电价求解 MILP，理论收益上限"),
    "gbm": ("GBM 预测 + 优化", "GBM forecast + optimiser", 1, "前一天用梯度提升树预测次日电价，按预测求解 MILP，按真实电价结算"),
    "persistence": ("昨日电价 + 优化", "Yesterday's prices + optimiser", 1, "把昨天的电价当作今天的预测"),
    "fixed": ("固定时段规则", "Fixed-schedule rule", 0, "每天 10–12 点充电、17–19 点放电，不用任何预测"),
}
SEASON = {12: "summer", 1: "summer", 2: "summer", 3: "autumn", 4: "autumn", 5: "autumn",
          6: "winter", 7: "winter", 8: "winter", 9: "spring", 10: "spring", 11: "spring"}
WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def connect(db_path: Path) -> sqlite3.Connection:
    """打开数据库。老版本 SQLite 没有 sqrt 等数学函数时，用 Python 函数补上，保证查询在 Windows 上也能跑。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    for name, fn in (("sqrt", math.sqrt), ("ln", math.log), ("power", math.pow)):
        try:
            conn.execute(f"SELECT {name}(4)" if name != "power" else "SELECT power(2, 2)")
        except sqlite3.OperationalError:
            conn.create_function(name, 1 if name != "power" else 2, fn, deterministic=True)
    return conn


# --------------------------------------------------------------------------- 维度表
def _dim_date_rows(first: pd.Timestamp, last: pd.Timestamp) -> list[tuple]:
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        rows.append((d.strftime("%Y-%m-%d"), d.year, (d.month - 1) // 3 + 1, d.month, d.strftime("%Y-%m"), d.day,
                     d.weekday(), WEEKDAY[d.weekday()], int(d.weekday() >= 5), SEASON[d.month]))
    return rows


def _dim_interval_rows() -> list[tuple]:
    rows = []
    for s in range(D.INTERVALS_PER_DAY):
        start = s * 5
        end = start + 5
        h, m = divmod(start, 60)
        peak = int(17 * 60 <= start < 21 * 60)
        trough = int(10 * 60 <= start < 15 * 60)
        period = "peak" if peak else ("trough" if trough else "shoulder")
        rows.append((s, f"{h:02d}:{m:02d}", f"{end // 60 % 24:02d}:{end % 60:02d}", h, m, peak, trough, period))
    return rows


# --------------------------------------------------------------------------- 事实表
def _load_dispatch(outputs: Path) -> list[tuple]:
    rows = []
    for k in STRATEGIES:
        f = outputs / "nem" / f"daily_{k}.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f, parse_dates=["trade_date"])
        rows += [(d.strftime("%Y-%m-%d"), k, float(r.revenue), float(r.charge_mwh), float(r.discharge_mwh), float(r.cycles))
                 for d, r in zip(df.trade_date, df.itertuples())]
    return rows


def _load_scenarios(outputs: Path) -> list[tuple]:
    rows, base = [], None

    def row(name, label, params, m, capex, note):
        g = m["gbm"]
        return (name, label, params["hours"], params["max_cycles_per_day"], params["rte"],
                m["perfect"]["revenue_k_per_mw"], g["revenue_k_per_mw"], g["capture_rate"], g["cycles_per_year"],
                g["net_k_per_mw"], capex, m["irr"]["gross_p50"], m["irr"]["net_p50"], note)

    for f in sorted((outputs / "scenarios").glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        rows.append(row(r["name"], r["label"].replace(" / RTE 86%", ""), r["params"], r["scenario"],
                        r["scenario"]["capex_mid_aud_m"], r.get("note") or None))
        if base is None:  # 每个情景文件都带同一个基准，取一次即可
            base = row("baseline", "基准：2h / 1.5 循环", dict(hours=2.0, max_cycles_per_day=1.5, rte=0.86),
                       r["baseline"], r["baseline"]["capex_mid_aud_m"], None)
    return ([base] if base else []) + rows


# --------------------------------------------------------------------------- 数据质量
# 每项检查是一条 SQL，返回 (actual, expected) 两个数；相等（允许极小误差）即通过。
QUALITY_CHECKS = [
    ("每个交易日正好 288 个区间（不等的天数）", "fact_price",
     "SELECT COUNT(*), 0 FROM (SELECT trade_date FROM fact_price GROUP BY trade_date HAVING COUNT(*) <> 288)"),
    ("总行数 = 交易日数 × 288", "fact_price",
     "SELECT COUNT(*), COUNT(DISTINCT trade_date) * 288 FROM fact_price"),
    ("交易日连续，没有缺失的日子", "fact_price",
     "SELECT COUNT(DISTINCT trade_date), julianday(MAX(trade_date)) - julianday(MIN(trade_date)) + 1 FROM fact_price"),
    ("rrp 空值", "fact_price", "SELECT SUM(rrp IS NULL), 0 FROM fact_price"),
    ("demand 空值", "fact_price", "SELECT SUM(demand IS NULL), 0 FROM fact_price"),
    ("主键 (trade_date, slot) 重复", "fact_price",
     "SELECT COUNT(*), 0 FROM (SELECT 1 FROM fact_price GROUP BY trade_date, slot HAVING COUNT(*) > 1)"),
    ("slot 超出 0-287", "fact_price", "SELECT SUM(slot NOT BETWEEN 0 AND 287), 0 FROM fact_price"),
    ("rrp 超出 NEM 价格上下限 [-1000, 20300]", "fact_price",
     "SELECT SUM(rrp < -1000 OR rrp > 20300), 0 FROM fact_price"),
    ("孤儿日期：fact_price 有、dim_date 没有", "fact_price",
     "SELECT COUNT(*), 0 FROM (SELECT DISTINCT p.trade_date FROM fact_price p "
     "LEFT JOIN dim_date d ON d.trade_date = p.trade_date WHERE d.trade_date IS NULL)"),
    ("各策略回测天数一致（不同天数的种类数）", "fact_dispatch_daily",
     "SELECT COUNT(DISTINCT n), 1 FROM (SELECT strategy, COUNT(*) AS n FROM fact_dispatch_daily GROUP BY strategy)"),
    ("回测日期都有对应电价（缺电价的回测天数）", "fact_dispatch_daily",
     "SELECT COUNT(*), 0 FROM (SELECT DISTINCT f.trade_date FROM fact_dispatch_daily f "
     "WHERE NOT EXISTS (SELECT 1 FROM fact_price p WHERE p.trade_date = f.trade_date))"),
    ("循环次数在 [0, 1.5] 内（超出的行数）", "fact_dispatch_daily",
     "SELECT SUM(cycles < -1e-6 OR cycles > 1.5 + 1e-6), 0 FROM fact_dispatch_daily"),
]


def run_quality_checks(conn: sqlite3.Connection) -> pd.DataFrame:
    """执行全部检查，写入 data_quality 表并返回结果。"""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM data_quality")
    rows = []
    for name, table, sql in QUALITY_CHECKS:
        actual, expected = conn.execute(sql).fetchone()
        actual = 0 if actual is None else actual           # SUM 在空表上返回 NULL
        passed = int(expected is not None and abs(actual - expected) < 1e-6)
        rows.append((name, table, expected, actual, passed, now))
    conn.executemany("INSERT INTO data_quality (check_name, table_name, expected, actual, passed, checked_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return pd.read_sql_query("SELECT check_id, table_name, check_name, expected, actual, passed FROM data_quality", conn)


# --------------------------------------------------------------------------- 一键建库
def build(db_path: Path, raw_dir: Path, outputs: Path, region: str = "QLD1", log=print) -> dict:
    """删除旧库 → 建表/索引/视图 → 写入维度表和事实表 → 数据质量检查 → ANALYZE。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        try:
            db_path.unlink()
        except PermissionError as e:  # Windows 上数据库被其他程序打开时删不掉
            raise SystemExit(f"无法覆盖 {db_path}：请先关闭正在打开它的程序（如 DB Browser）。") from e

    notes: list[str] = []
    df = D.load(Path(raw_dir), region, log=notes)
    for n in notes:
        log(f"[数据] {n}")
    dispatch = _load_dispatch(Path(outputs))
    scenarios = _load_scenarios(Path(outputs))

    first = df.trade_date.min()
    last = df.trade_date.max()
    if dispatch:
        first = min(first, pd.Timestamp(min(r[0] for r in dispatch)))
        last = max(last, pd.Timestamp(max(r[0] for r in dispatch)))

    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        # 先写维度表，再写事实表（外键要求被引用的行先存在）
        conn.executemany("INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _dim_date_rows(first, last))
        conn.executemany("INSERT INTO dim_interval VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _dim_interval_rows())
        conn.executemany("INSERT INTO dim_strategy VALUES (?, ?, ?, ?, ?)",
                         [(k, *v) for k, v in STRATEGIES.items()])
        price_rows = list(zip(df.trade_date.dt.strftime("%Y-%m-%d"), df.slot.astype(int),
                              df.rrp.astype(float), df.demand.astype(float)))
        conn.executemany("INSERT INTO fact_price VALUES (?, ?, ?, ?)", price_rows)
        conn.executemany("INSERT INTO fact_dispatch_daily VALUES (?, ?, ?, ?, ?, ?)", dispatch)
        conn.executemany("INSERT INTO fact_scenario VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", scenarios)
        conn.commit()
        quality = run_quality_checks(conn)
        conn.execute("ANALYZE")  # 收集统计信息，让查询优化器选对索引
        conn.commit()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("dim_date", "dim_interval", "dim_strategy", "fact_price", "fact_dispatch_daily", "fact_scenario")}
    finally:
        conn.close()

    log(f"[建库] {db_path}")
    for t, n in counts.items():
        log(f"  {t:<22}{n:>10,} 行")
    log("[数据质量]")
    for r in quality.itertuples():
        mark = "通过" if r.passed else "未通过"
        log(f"  [{mark}] {r.table_name:<20} {r.check_name}（实际 {r.actual:g}，应为 {r.expected:g}）")
    n_fail = int((quality.passed == 0).sum())
    log(f"[数据质量] {len(quality) - n_fail}/{len(quality)} 项通过" + ("" if not n_fail else "，请检查上面未通过的项"))
    return dict(db=str(db_path), counts=counts, quality_passed=n_fail == 0, quality=quality)
