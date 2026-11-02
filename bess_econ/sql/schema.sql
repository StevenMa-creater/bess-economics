-- =====================================================================
-- bess-economics 分析型数据库（SQLite）
--
-- 设计思路：星型模型（star schema）
--   事实表 fact_*：记录"发生了什么"，行数多，主要是数字（电价、负荷、收入）。
--   维度表 dim_*：描述"在什么条件下发生"，行数少，主要是属性（哪天、几点、哪种策略）。
--   分析时用 JOIN 把事实表和维度表连起来，再按维度属性 GROUP BY。
--
-- SQLite 没有专门的日期类型，日期统一存成 'YYYY-MM-DD' 文本；
-- 这种格式按字符串排序就是按时间排序，也能直接用 strftime / julianday 计算。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 维度表 1：交易日
-- ---------------------------------------------------------------------
CREATE TABLE dim_date (
    trade_date   TEXT    PRIMARY KEY,          -- 交易日 'YYYY-MM-DD'（AEST，按区间开始时刻划分）
    year         INTEGER NOT NULL,
    quarter      INTEGER NOT NULL,             -- 1–4
    month        INTEGER NOT NULL,             -- 1–12
    year_month   TEXT    NOT NULL,             -- 'YYYY-MM'，按月汇总时最常用
    day          INTEGER NOT NULL,
    weekday      INTEGER NOT NULL,             -- 0 = 周一 … 6 = 周日
    weekday_name TEXT    NOT NULL,             -- 'Mon' … 'Sun'
    is_weekend   INTEGER NOT NULL CHECK (is_weekend IN (0, 1)),
    season       TEXT    NOT NULL              -- 南半球季节：summer(12–2) / autumn(3–5) / winter(6–8) / spring(9–11)
);

-- ---------------------------------------------------------------------
-- 维度表 2：一天中的 5 分钟区间（288 个）
-- ---------------------------------------------------------------------
CREATE TABLE dim_interval (
    slot       INTEGER PRIMARY KEY CHECK (slot BETWEEN 0 AND 287),  -- 0 = 00:00–00:05
    start_time TEXT    NOT NULL,               -- 'HH:MM'，区间开始时刻
    end_time   TEXT    NOT NULL,               -- 'HH:MM'，区间结束时刻（= AEMO 的 SETTLEMENTDATE）
    hour       INTEGER NOT NULL,               -- 0–23
    minute     INTEGER NOT NULL,
    is_peak    INTEGER NOT NULL CHECK (is_peak IN (0, 1)),    -- 晚高峰 17:00–21:00
    is_trough  INTEGER NOT NULL CHECK (is_trough IN (0, 1)),  -- 午间光伏低谷 10:00–15:00
    period     TEXT    NOT NULL                -- 'peak' / 'trough' / 'shoulder'
);

-- ---------------------------------------------------------------------
-- 维度表 3：调度策略
-- ---------------------------------------------------------------------
CREATE TABLE dim_strategy (
    strategy    TEXT PRIMARY KEY,              -- perfect / gbm / persistence / fixed
    name_zh     TEXT NOT NULL,
    name_en     TEXT NOT NULL,
    uses_forecast INTEGER NOT NULL CHECK (uses_forecast IN (0, 1)),
    description TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- 事实表 1：5 分钟电价与负荷（粒度：交易日 × 区间）
-- 复合主键 (trade_date, slot) 保证同一区间只有一行，SQLite 会自动为它建索引。
-- ---------------------------------------------------------------------
CREATE TABLE fact_price (
    trade_date TEXT    NOT NULL REFERENCES dim_date (trade_date),
    slot       INTEGER NOT NULL REFERENCES dim_interval (slot),
    rrp        REAL    NOT NULL,               -- 区域参考电价 A$/MWh（可以为负）
    demand     REAL,                           -- 区域总负荷 MW
    PRIMARY KEY (trade_date, slot)
);

-- ---------------------------------------------------------------------
-- 事实表 2：逐日调度回测结果（粒度：交易日 × 策略），来自 outputs/nem/daily_*.csv
-- 电池 100 MW / 200 MWh；revenue 单位 A$（整块电池，不是每 MW）。
-- ---------------------------------------------------------------------
CREATE TABLE fact_dispatch_daily (
    trade_date    TEXT NOT NULL REFERENCES dim_date (trade_date),
    strategy      TEXT NOT NULL REFERENCES dim_strategy (strategy),
    revenue       REAL NOT NULL,               -- 当日套利收入 A$
    charge_mwh    REAL NOT NULL,               -- 从电网充电量
    discharge_mwh REAL NOT NULL,               -- 向电网放电量
    cycles        REAL NOT NULL,               -- 等效满循环次数
    PRIMARY KEY (trade_date, strategy)
);

-- ---------------------------------------------------------------------
-- 事实表 3：情景实验汇总（粒度：一个情景一行），来自 outputs/scenarios/*.json
-- 收入单位：千 A$ / MW / 年；IRR 为小数（0.048 = 4.8%）。
-- ---------------------------------------------------------------------
CREATE TABLE fact_scenario (
    scenario        TEXT PRIMARY KEY,          -- baseline / 4h / cycles_1.0 …
    label           TEXT NOT NULL,
    duration_h      REAL NOT NULL,
    max_cycles      REAL,                      -- 每日循环上限，NULL = 不限
    rte             REAL NOT NULL,
    perfect_rev_k   REAL NOT NULL,             -- 完美预知收入
    gbm_rev_k       REAL NOT NULL,             -- GBM 策略收入
    gbm_capture     REAL NOT NULL,             -- GBM 捕获率
    gbm_cycles      REAL NOT NULL,             -- GBM 年循环
    gbm_net_k       REAL NOT NULL,             -- 扣电芯损耗后的净收入
    capex_aud_m     REAL NOT NULL,             -- 投资（百万 A$，100 MW）
    irr_gross_p50   REAL NOT NULL,
    irr_net_p50     REAL NOT NULL,
    note            TEXT
);

-- ---------------------------------------------------------------------
-- 数据质量检查结果（建库时自动写入）
-- ---------------------------------------------------------------------
CREATE TABLE data_quality (
    check_id   INTEGER PRIMARY KEY,
    check_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    expected   REAL,
    actual     REAL,
    passed     INTEGER NOT NULL CHECK (passed IN (0, 1)),
    checked_at TEXT NOT NULL
);

-- =====================================================================
-- 索引
-- 主键已经自带索引。下面这些索引服务于常见的查询条件：
-- =====================================================================
-- 按电价筛选（如 rrp > 1000 的尖峰区间）：没有它就要扫描全部 21 万行
CREATE INDEX idx_price_rrp ON fact_price (rrp);
-- 按区间（一天中的时刻）分组或筛选，比如鸭子曲线、晚高峰
CREATE INDEX idx_price_slot ON fact_price (slot, trade_date);
-- 按策略取逐日收入（WHERE strategy = 'gbm' ORDER BY trade_date）
CREATE INDEX idx_dispatch_strategy ON fact_dispatch_daily (strategy, trade_date);
-- 按月汇总时用 year_month 关联
CREATE INDEX idx_date_year_month ON dim_date (year_month);

-- =====================================================================
-- 视图：把常用的查询存成"虚拟表"，用的时候像表一样 SELECT
-- =====================================================================

-- 视图 1：电价明细 + 日期、时刻属性（省去每次写两次 JOIN）
CREATE VIEW v_price AS
SELECT p.trade_date, p.slot, i.start_time, i.hour, i.period, i.is_peak, i.is_trough,
       d.year, d.month, d.year_month, d.weekday, d.is_weekend, d.season,
       p.rrp, p.demand
FROM fact_price p
JOIN dim_date d     ON d.trade_date = p.trade_date
JOIN dim_interval i ON i.slot = p.slot;

-- 视图 2：每日价差。最贵 24 个区间（2 小时）均价 − 最便宜 24 个区间均价，
-- 近似"一块 2 小时电池一天充放一次"能捕获的价差上限（不考虑时间先后）。
CREATE VIEW v_daily_spread AS
WITH ranked AS (
    SELECT trade_date, rrp,
           ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY rrp ASC)  AS rn_low,
           ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY rrp DESC) AS rn_high
    FROM fact_price
)
SELECT trade_date,
       AVG(CASE WHEN rn_high <= 24 THEN rrp END)                                   AS top2h_avg,
       AVG(CASE WHEN rn_low  <= 24 THEN rrp END)                                   AS bottom2h_avg,
       AVG(CASE WHEN rn_high <= 24 THEN rrp END) - AVG(CASE WHEN rn_low <= 24 THEN rrp END) AS spread_2h,
       AVG(rrp)                                                                    AS daily_mean,
       MAX(rrp)                                                                    AS daily_max,
       MIN(rrp)                                                                    AS daily_min,
       AVG(CASE WHEN rrp < 0 THEN 1.0 ELSE 0.0 END)                                AS neg_share
FROM ranked
GROUP BY trade_date;

-- 视图 3：月度汇总。电价统计 + 平均价差 + 完美预知和 GBM 收入（只有回测年份有收入，其他月份为 NULL）
CREATE VIEW v_monthly_summary AS
WITH price_m AS (
    SELECT d.year_month,
           COUNT(DISTINCT p.trade_date)                  AS days,
           AVG(p.rrp)                                    AS avg_rrp,
           MAX(p.rrp)                                    AS max_rrp,
           MIN(p.rrp)                                    AS min_rrp,
           AVG(CASE WHEN p.rrp < 0 THEN 1.0 ELSE 0.0 END) AS neg_share,
           AVG(p.demand)                                 AS avg_demand
    FROM fact_price p
    JOIN dim_date d ON d.trade_date = p.trade_date
    GROUP BY d.year_month
),
spread_m AS (
    SELECT d.year_month, AVG(s.spread_2h) AS avg_spread_2h
    FROM v_daily_spread s
    JOIN dim_date d ON d.trade_date = s.trade_date
    GROUP BY d.year_month
),
dispatch_m AS (
    SELECT d.year_month,
           SUM(CASE WHEN f.strategy = 'perfect' THEN f.revenue END) AS perfect_revenue,
           SUM(CASE WHEN f.strategy = 'gbm'     THEN f.revenue END) AS gbm_revenue
    FROM fact_dispatch_daily f
    JOIN dim_date d ON d.trade_date = f.trade_date
    GROUP BY d.year_month
)
SELECT p.year_month, p.days, p.avg_rrp, p.max_rrp, p.min_rrp, p.neg_share, p.avg_demand,
       s.avg_spread_2h,
       m.perfect_revenue, m.gbm_revenue,
       m.gbm_revenue / m.perfect_revenue AS gbm_capture
FROM price_m p
LEFT JOIN spread_m   s ON s.year_month = p.year_month
LEFT JOIN dispatch_m m ON m.year_month = p.year_month;
