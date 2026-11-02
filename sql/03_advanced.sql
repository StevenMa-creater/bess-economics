-- =====================================================================
-- L3 高级：gaps and islands / 分位数 / 事件关联 / 纯 SQL 策略回测 / 执行计划与索引
-- 运行：python run.py sql run --level 3
-- 注意：L3_07 会新建一张结果表 bt_sql_strategy_daily（纯 SQL 回测结果），后面几条查询要用到它。
-- =====================================================================


-- @name: L3_01_negative_runs_summary
-- 业务问题：负电价通常连续持续多久？最长一次持续了几个小时？
-- 知识点：经典的 gaps and islands（找"连续段"）问题。
--        第 1 步：给每个区间一个全局连续编号 idx = 天数偏移 × 288 + slot（julianday 把日期变成数字）；
--        第 2 步：只看负电价的行，按 idx 排序编号 rn；
--        第 3 步：同一段连续负电价里，idx 和 rn 同步加 1，所以 idx − rn 是常数 → 用它当"段号"分组。
--        例：idx = 100,101,102, 200,201 → rn = 1,2,3,4,5 → idx − rn = 99,99,99, 196,196 → 两段。
-- 结果解读：实际结果：两年共 2,836 段负电价，平均 1.1 小时，最长一段 11.7 小时（2025-12-02 05:45–17:25）；
--          时长 ≥ 2 小时的段只有 407 段，却占了 80% 的负电价时间。
--          连续负电价段越长，电池越容易在负价时段充满；但段长超过电池充满所需时间（约 2 小时）后，
--          多出来的负价时段电池已经吃不下了，这部分只能靠更多储能、更长时长或需求响应来消纳。
WITH idx AS (
    SELECT trade_date, slot, rrp,
           CAST((julianday(trade_date) - julianday('2024-01-01')) AS INTEGER) * 288 + slot AS idx
    FROM fact_price
),
neg AS (
    SELECT idx, trade_date, slot,
           idx - ROW_NUMBER() OVER (ORDER BY idx) AS grp
    FROM idx
    WHERE rrp < 0
),
runs AS (
    SELECT grp, MIN(idx) AS start_idx, COUNT(*) AS n_intervals
    FROM neg
    GROUP BY grp
)
SELECT COUNT(*)                                        AS n_runs,
       ROUND(MAX(n_intervals) / 12.0, 1)               AS longest_hours,
       ROUND(AVG(n_intervals) / 12.0, 2)               AS avg_hours,
       SUM(n_intervals >= 24)                          AS runs_over_2h,
       SUM(n_intervals >= 72)                          AS runs_over_6h,
       ROUND(SUM(CASE WHEN n_intervals >= 24 THEN n_intervals END) * 100.0 / SUM(n_intervals), 1)
                                                       AS pct_neg_time_in_runs_over_2h
FROM runs;


-- @name: L3_02_longest_negative_runs
-- 业务问题：最长的 10 段连续负电价分别从几点到几点？
-- 知识点：在 L3_01 的基础上，取每段的第一行和最后一行，再 JOIN dim_interval 把 slot 翻译成时刻。
-- 结果解读：最长的负价段基本都在春季（9–11 月）和周末：光伏出力强、空调负荷低、工厂休息。
WITH idx AS (
    SELECT trade_date, slot, rrp,
           CAST((julianday(trade_date) - julianday('2024-01-01')) AS INTEGER) * 288 + slot AS idx
    FROM fact_price
),
neg AS (
    SELECT idx, trade_date, slot, rrp, idx - ROW_NUMBER() OVER (ORDER BY idx) AS grp
    FROM idx
    WHERE rrp < 0
),
runs AS (
    SELECT grp, MIN(idx) AS start_idx, MAX(idx) AS end_idx, COUNT(*) AS n_intervals, AVG(rrp) AS avg_rrp
    FROM neg
    GROUP BY grp
)
SELECT s.trade_date, d.weekday_name, i1.start_time, i2.end_time,
       ROUND(r.n_intervals / 12.0, 1) AS hours, ROUND(r.avg_rrp, 1) AS avg_rrp
FROM runs r
JOIN neg s           ON s.idx = r.start_idx
JOIN neg e           ON e.idx = r.end_idx
JOIN dim_interval i1 ON i1.slot = s.slot
JOIN dim_interval i2 ON i2.slot = e.slot
JOIN dim_date d      ON d.trade_date = s.trade_date
ORDER BY r.n_intervals DESC
LIMIT 10;


-- @name: L3_03_negative_runs_by_month
-- 业务问题：每个月有多少段负电价？平均多长？最长多长？负电价总时长多少？
-- 知识点：gaps and islands 的结果再按月 GROUP BY；段的月份取开始那一天的月份。
-- 结果解读：负价段数和时长的季节规律，决定电池"免费充电"机会在一年里怎么分布；
--          和 L2_01 的月度价差对照：负电价多的月份价差未必大，还要看傍晚峰值高不高。
WITH idx AS (
    SELECT trade_date, slot, rrp,
           CAST((julianday(trade_date) - julianday('2024-01-01')) AS INTEGER) * 288 + slot AS idx
    FROM fact_price
),
neg AS (
    SELECT idx, trade_date, idx - ROW_NUMBER() OVER (ORDER BY idx) AS grp
    FROM idx
    WHERE rrp < 0
),
runs AS (
    SELECT grp, MIN(trade_date) AS start_date, COUNT(*) AS n_intervals
    FROM neg
    GROUP BY grp
)
SELECT strftime('%Y-%m', start_date)         AS year_month,
       COUNT(*)                              AS n_runs,
       ROUND(AVG(n_intervals) / 12.0, 2)     AS avg_hours,
       ROUND(MAX(n_intervals) / 12.0, 1)     AS longest_hours,
       ROUND(SUM(n_intervals) / 12.0, 0)     AS total_neg_hours
FROM runs
GROUP BY year_month
ORDER BY year_month;


-- @name: L3_04_price_duration_curve
-- 业务问题：价格持续曲线：一年中有 1%、5%、10%……的时间电价高于多少？
-- 知识点：NTILE(100) OVER (PARTITION BY 年 ORDER BY 电价 DESC) 把每年的区间按电价从高到低等分成 100 份；
--        第 k 份里的最低价 ≈ "一年中 k% 的时间电价高于这个值"。SQLite 没有 PERCENTILE 函数，这是常用替代写法。
--        外层用条件聚合把 2024、2025 两年并成两列，便于比较。
-- 结果解读：实际结果：前 5% 时间的门槛价从 2024 年的 A$272 降到 2025 年的 A$199，
--          后 10% 从 −25 变成 −16，两端都在向中间收拢。
--          曲线左端（前 1%）是尖峰区，决定储能的"彩票收入"；右端（后 20%）是负价区，决定充电成本。
--          两年对比，如果中间段变化不大而两端变平，说明储能增多后尖峰被削、负价被填。
WITH t AS (
    SELECT strftime('%Y', trade_date) AS year, rrp,
           NTILE(100) OVER (PARTITION BY strftime('%Y', trade_date) ORDER BY rrp DESC) AS pct
    FROM fact_price
),
p AS (
    SELECT year, pct, MIN(rrp) AS price_at_pct
    FROM t
    GROUP BY year, pct
)
SELECT pct                                                         AS pct_of_time_above,
       ROUND(MAX(CASE WHEN year = '2024' THEN price_at_pct END), 1) AS price_2024,
       ROUND(MAX(CASE WHEN year = '2025' THEN price_at_pct END), 1) AS price_2025
FROM p
WHERE pct IN (1, 2, 5, 10, 20, 30, 50, 70, 80, 90, 95, 99)
GROUP BY pct
ORDER BY pct;


-- @name: L3_05_spike_days
-- 业务问题：电价超过 A$1,000/MWh 的尖峰区间集中在哪些天？这些天完美预知策略赚了多少？
-- 知识点：先 WHERE 过滤（只留尖峰区间，可以用上 rrp 索引），再 GROUP BY 按天聚合；
--        INNER JOIN 回测结果：只保留两边都有的日期（回测只有 2025 年，2024 年的尖峰日自动被去掉；
--        想保留 2024 年就改成 LEFT JOIN，收入列会是 NULL）；子查询算当天收入占全年比例。
-- 结果解读：一天里哪怕只有十几分钟的尖峰，收入就可能抵得上平常几十天；
--          尖峰日收入占比 = 储能收入对极端事件的依赖程度，是投资风险的核心来源。
WITH spikes AS (
    SELECT trade_date,
           COUNT(*)        AS spike_intervals,
           MAX(rrp)        AS max_rrp,
           AVG(rrp)        AS avg_spike_rrp
    FROM fact_price
    WHERE rrp > 1000
    GROUP BY trade_date
)
SELECT s.trade_date, s.spike_intervals, ROUND(s.spike_intervals * 5.0, 0) AS spike_minutes,
       ROUND(s.max_rrp, 0) AS max_rrp,
       ROUND(f.revenue / 1000, 0) AS perfect_revenue_k,
       ROUND(f.revenue * 100.0 / (SELECT SUM(revenue) FROM fact_dispatch_daily WHERE strategy = 'perfect'), 1)
                                   AS pct_of_annual_perfect
FROM spikes s
JOIN fact_dispatch_daily f ON f.trade_date = s.trade_date AND f.strategy = 'perfect'
ORDER BY f.revenue DESC;


-- @name: L3_06_spike_vs_normal_days
-- 业务问题：2025 年有尖峰的日子和没有尖峰的日子，平均每天收入差多少？尖峰日贡献了多少收入？
-- 知识点：EXISTS 子查询判断"当天是否存在 rrp > 1000 的区间"（找到一行就停，比 COUNT 更快）；
--        按布尔结果分组。
-- 结果解读：实际结果：2025 年 18 个尖峰日贡献了完美预知全年收入的 37%，平均每天 A$421k，普通日只有 A$37k。
--          尖峰日只占全年几天，但每天的收入是普通日的几十倍；GBM 在尖峰日的捕获率比普通日低，
--          因为尖峰本身很难提前一天预测到。
WITH flagged AS (
    SELECT f.trade_date, f.strategy, f.revenue,
           EXISTS (SELECT 1 FROM fact_price p WHERE p.trade_date = f.trade_date AND p.rrp > 1000) AS is_spike_day
    FROM fact_dispatch_daily f
    WHERE f.strategy IN ('perfect', 'gbm')
)
SELECT CASE WHEN is_spike_day THEN '尖峰日' ELSE '普通日' END                   AS day_type,
       COUNT(DISTINCT trade_date)                                              AS days,
       ROUND(AVG(CASE WHEN strategy = 'perfect' THEN revenue END) / 1000, 1)   AS perfect_avg_k,
       ROUND(AVG(CASE WHEN strategy = 'gbm'     THEN revenue END) / 1000, 1)   AS gbm_avg_k,
       ROUND(SUM(CASE WHEN strategy = 'gbm' THEN revenue END)
             / SUM(CASE WHEN strategy = 'perfect' THEN revenue END) * 100, 1)  AS gbm_capture_pct,
       ROUND(SUM(CASE WHEN strategy = 'perfect' THEN revenue END) * 100.0
             / (SELECT SUM(revenue) FROM fact_dispatch_daily WHERE strategy = 'perfect'), 1) AS pct_of_perfect_revenue
FROM flagged
GROUP BY is_spike_day
ORDER BY is_spike_day DESC;


-- @name: L3_07_sql_strategy_backtest
-- 业务问题：不用优化器、只用 SQL，能做出多好的套利策略？（纯 SQL 回测，结果写入表 bt_sql_strategy_daily）
-- 知识点：参数 CTE（把电池参数集中写在一处）；ROW_NUMBER 选最便宜 / 最贵的 24 个区间；
--        滚动窗口 AVG … ROWS BETWEEN CURRENT ROW AND 23 FOLLOWING 算每个起点开始的连续 2 小时均价；
--        窗口帧 ROWS BETWEEN UNBOUNDED PRECEDING AND 24 PRECEDING 求"在放电窗口开始之前、最便宜的充电窗口"；
--        累计求和 SUM … ROWS UNBOUNDED PRECEDING 模拟 SOC，检查是否"先放后充"；CREATE TABLE … AS 保存结果。
-- 电池和能量口径与项目的固定时段规则一致：100 MW / 200 MWh，SOC 5%–95%（每次循环电池侧 180 MWh），
--        往返效率 86%（单程 √0.86 ≈ 0.927）：每天从电网充 180 / 0.927 = 194.1 MWh，放给电网 180 × 0.927 = 166.9 MWh，
--        各分摊到 24 个区间（2 小时），平均充电 97 MW、放电 83 MW，都不超过 100 MW。
-- 两个版本：
--   A. 无序版（naive）：最便宜的 24 个区间充电、最贵的 24 个区间放电，不管时间先后。
--      可能出现"先放电后充电"，物理上做不到，所以是乐观估计；feasible 列标出当天是否可行。
--   B. 有序版（ordered）：一个连续 2 小时充电窗口，必须在一个连续 2 小时放电窗口之前；
--      在所有可行的组合里选价差最大的一对。这相当于"事后选出最佳时段的固定规则"，物理上可行。
--   当天最佳组合也亏钱时就不交易（收入记 0）。
-- 结果解读：表里每天一行，记录两个版本的收入、有序版的充放电开始时刻、无序版是否可行；
--          已用 numpy 逐日暴力枚举核对过，365 天结果一致。实际结果：有序版 A$162.5k/MW/年，无序版 175.7k（但 195 天不可行）。
DROP TABLE IF EXISTS bt_sql_strategy_daily;
CREATE TABLE bt_sql_strategy_daily AS
WITH params AS (
    SELECT 180.0 AS usable_mwh, sqrt(0.86) AS eta
),
px AS (                                   -- 只回测有 MILP 结果的日期（2025 年），便于对比
    SELECT p.trade_date, p.slot, p.rrp
    FROM fact_price p
    WHERE p.trade_date IN (SELECT DISTINCT trade_date FROM fact_dispatch_daily)
),
-- ---------- A. 无序版 ----------
ranked AS (
    SELECT trade_date, slot, rrp,
           ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY rrp ASC,  slot) AS rn_low,
           ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY rrp DESC, slot) AS rn_high
    FROM px
),
soc_path AS (                             -- 每个区间电池侧能量变化：充 +7.5 MWh，放 −7.5 MWh，累计求和 = SOC
    SELECT trade_date, slot,
           SUM(CASE WHEN rn_low <= 24 THEN 7.5 WHEN rn_high <= 24 THEN -7.5 ELSE 0 END)
               OVER (PARTITION BY trade_date ORDER BY slot ROWS UNBOUNDED PRECEDING) AS soc_change
    FROM ranked
),
naive AS (
    SELECT r.trade_date,
           AVG(CASE WHEN r.rn_low  <= 24 THEN r.rrp END) AS avg_charge_price,
           AVG(CASE WHEN r.rn_high <= 24 THEN r.rrp END) AS avg_discharge_price
    FROM ranked r
    GROUP BY r.trade_date
),
naive_ok AS (
    SELECT trade_date, MIN(soc_change) >= -1e-9 AS feasible   -- SOC 从未低于起点 = 没有先放后充
    FROM soc_path
    GROUP BY trade_date
),
-- ---------- B. 有序版 ----------
win AS (                                  -- 每个起点 slot 开始的连续 24 个区间均价
    SELECT trade_date, slot AS start_slot,
           AVG(rrp) OVER (PARTITION BY trade_date ORDER BY slot ROWS BETWEEN CURRENT ROW AND 23 FOLLOWING) AS avg24,
           COUNT(*) OVER (PARTITION BY trade_date ORDER BY slot ROWS BETWEEN CURRENT ROW AND 23 FOLLOWING) AS n
    FROM px
),
win24 AS (
    SELECT trade_date, start_slot, avg24 FROM win WHERE n = 24   -- 起点 0–264，保证窗口在当天内
),
best_before AS (                          -- 对每个放电窗口，找结束于它开始之前的最便宜充电窗口
    SELECT trade_date, start_slot AS dis_slot, avg24 AS avg_dis,
           MIN(avg24) OVER (PARTITION BY trade_date ORDER BY start_slot
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 24 PRECEDING) AS best_charge
    FROM win24
),
pairs AS (                                -- 每个放电窗口配上最佳充电窗口后的当日利润（A$）
    SELECT b.trade_date, b.dis_slot, b.avg_dis, b.best_charge,
           pr.usable_mwh * pr.eta * b.avg_dis - pr.usable_mwh / pr.eta * b.best_charge AS profit
    FROM best_before b
    CROSS JOIN params pr                  -- params 只有一行，CROSS JOIN 相当于把参数贴到每一行上
    WHERE b.best_charge IS NOT NULL       -- 放电窗口太早（前面放不下 2 小时充电）时没有可选的充电窗口
),
best_pair AS (                            -- 每天利润最高的那一对
    SELECT *
    FROM (SELECT pairs.*, ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY profit DESC) AS rk FROM pairs)
    WHERE rk = 1
),
best_pair_slots AS (                      -- 找回最便宜充电窗口的起点（同价取最晚的那个）
    SELECT bp.trade_date, bp.dis_slot, bp.avg_dis, bp.best_charge, bp.profit,
           MAX(w.start_slot) AS chg_slot
    FROM best_pair bp
    JOIN win24 w ON w.trade_date = bp.trade_date
                AND w.start_slot <= bp.dis_slot - 24
                AND w.avg24 = bp.best_charge
    GROUP BY bp.trade_date
)
SELECT n.trade_date,
       ROUND(n.avg_charge_price, 4)                                       AS naive_charge_price,
       ROUND(n.avg_discharge_price, 4)                                    AS naive_discharge_price,
       MAX(pr.usable_mwh * pr.eta * n.avg_discharge_price
         - pr.usable_mwh / pr.eta * n.avg_charge_price, 0)               AS naive_revenue,
       k.feasible                                                         AS naive_feasible,
       o.chg_slot                                                         AS ordered_charge_slot,
       o.dis_slot                                                         AS ordered_discharge_slot,
       ROUND(o.best_charge, 4)                                            AS ordered_charge_price,
       ROUND(o.avg_dis, 4)                                                AS ordered_discharge_price,
       MAX(o.profit, 0)                                                   AS ordered_revenue
FROM naive n
CROSS JOIN params pr
JOIN naive_ok k        ON k.trade_date = n.trade_date
JOIN best_pair_slots o ON o.trade_date = n.trade_date
ORDER BY n.trade_date;
-- 预览前几天：充电、放电窗口的开始时刻和收入
SELECT b.trade_date, ic.start_time AS charge_from, id.start_time AS discharge_from,
       ROUND(b.ordered_revenue, 0) AS ordered_revenue, ROUND(b.naive_revenue, 0) AS naive_revenue, b.naive_feasible
FROM bt_sql_strategy_daily b
JOIN dim_interval ic ON ic.slot = b.ordered_charge_slot
JOIN dim_interval id ON id.slot = b.ordered_discharge_slot
ORDER BY b.trade_date;


-- @name: L3_08_sql_vs_milp
-- 业务问题：纯 SQL 策略和 MILP 优化（完美预知、GBM）比，收益捕获率分别是多少？
-- 知识点：UNION ALL 把不同来源的结果上下拼成一张表；标量子查询取完美预知总收入当分母。
-- 结果解读：实际结果：SQL 有序版捕获 79.6%，比只用预测的 GBM（76.3%）略高——
--          说明 GBM 不看未来电价，已经接近"事后挑最佳 2 小时窗口"的水平。
--          有序版 SQL 策略用了"事后看到的真实电价"，
--          仍然明显低于 MILP 完美预知，差距就是优化器的价值：每天可以多次充放、按价格灵活调整功率、
--          利用起始 SOC，而不是死板的"2 小时充、2 小时放"。
WITH perfect AS (
    SELECT SUM(revenue) AS total FROM fact_dispatch_daily WHERE strategy = 'perfect'
),
all_strats AS (
    SELECT s.name_zh AS strategy, SUM(f.revenue) AS revenue, SUM(f.cycles) AS cycles, s.uses_forecast AS uses_forecast
    FROM fact_dispatch_daily f
    JOIN dim_strategy s ON s.strategy = f.strategy
    GROUP BY s.strategy, s.name_zh, s.uses_forecast
    UNION ALL
    SELECT 'SQL 有序版（事后最佳 2h 窗口）', SUM(ordered_revenue), SUM(ordered_revenue > 0), 0
    FROM bt_sql_strategy_daily
    UNION ALL
    SELECT 'SQL 无序版（不计先后，乐观）', SUM(naive_revenue), SUM(naive_revenue > 0), 0
    FROM bt_sql_strategy_daily
)
SELECT strategy,
       ROUND(revenue / 100 / 1000, 1)                         AS revenue_k_per_mw,
       ROUND(revenue / (SELECT total FROM perfect) * 100, 1)  AS capture_pct,
       ROUND(cycles, 0)                                       AS cycles,
       CASE WHEN uses_forecast = 1 THEN '否（只用预测）' ELSE '是（事后真实电价）' END AS sees_actual_prices
FROM all_strats
ORDER BY revenue DESC;


-- @name: L3_09_why_milp_wins
-- 业务问题：MILP 完美预知比纯 SQL 策略多赚的钱从哪来？能不能拆成几步解释？
-- 知识点：用 UNION ALL 拼出一张"瀑布表"，每一行是去掉一种灵活性后的策略；
--        标量子查询分别从 fact_dispatch_daily、fact_scenario（情景实验：循环上限 1 次）、bt_sql_strategy_daily 取收入；
--        LAG 计算相邻两步的差值，FIRST_VALUE 取第一行做分母算捕获率。
-- 结果解读：实际结果（千 A$/MW/年）：204.2 → 180.8 → 162.5 → 139.0，三步分别少 23.4、18.3、23.5。
--          从上往下每一步都去掉一种灵活性，差值就是这种灵活性值多少钱：
--   第 1→2 步：每天多做半次循环的价值（早晚两个高峰都能吃到）；
--   第 2→3 步：同样每天 1 次循环，MILP 可以分段充放、按价格调功率，SQL 版只能"连续 2 小时、恒定功率"；
--   第 3→4 步：SQL 版每天事后挑最佳时段，固定规则每天都是 10–12 点充、17–19 点放。
WITH steps AS (
    SELECT 1 AS step, 'MILP 完美预知（每天最多 1.5 次循环）' AS strategy,
           (SELECT SUM(revenue) FROM fact_dispatch_daily WHERE strategy = 'perfect') / 100 / 1000 AS revenue_k_per_mw,
           '基准：理论上限' AS what_is_removed
    UNION ALL
    SELECT 2, 'MILP 完美预知（每天最多 1 次循环）',
           (SELECT perfect_rev_k FROM fact_scenario WHERE scenario = 'cycles_1.0'),
           '去掉每天第二个半次循环'
    UNION ALL
    SELECT 3, 'SQL 有序版（1 次循环，连续 2 小时窗口）',
           (SELECT SUM(ordered_revenue) FROM bt_sql_strategy_daily) / 100 / 1000,
           '去掉分段充放和按价格调功率'
    UNION ALL
    SELECT 4, '固定时段规则（10–12 点充，17–19 点放）',
           (SELECT SUM(revenue) FROM fact_dispatch_daily WHERE strategy = 'fixed') / 100 / 1000,
           '去掉每天事后挑最佳时段'
)
SELECT step, strategy,
       ROUND(revenue_k_per_mw, 1)                                                          AS revenue_k_per_mw,
       ROUND(revenue_k_per_mw - LAG(revenue_k_per_mw) OVER (ORDER BY step), 1)             AS change_k_per_mw,
       ROUND(revenue_k_per_mw / FIRST_VALUE(revenue_k_per_mw) OVER (ORDER BY step) * 100, 1) AS capture_pct,
       what_is_removed
FROM steps
ORDER BY step;


-- @name: L3_10_sql_strategy_checks
-- 业务问题：无序版有多少天其实做不到（要先放电后充电）？SQL 有序版有没有哪天比 MILP 完美预知还高？为什么？
-- 知识点：JOIN 两个来源后用条件聚合一次输出多个检查结果；SUM(条件) 计数。
-- 结果解读：无序版不可行的天数说明"只看价差、不看时间先后"会高估收入，高估的金额见 overstated_k。
--          有序版偶尔高于 MILP，是因为两者假设不同：MILP 规定每天从 50% SOC 开始并回到 50%（日与日解耦），
--          SQL 版假设每天从空电池开始；如果把 MILP 的起点也设成 5%，SQL 有序版一定不会更高
--          （tests/test_sql.py 用这个性质验证 SQL 回测的正确性）。
WITH j AS (
    SELECT b.trade_date, b.ordered_revenue, b.naive_revenue, b.naive_feasible, p.revenue AS perfect_revenue
    FROM bt_sql_strategy_daily b
    JOIN fact_dispatch_daily p ON p.trade_date = b.trade_date AND p.strategy = 'perfect'
)
SELECT COUNT(*)                                                                     AS days,
       SUM(naive_feasible = 0)                                                      AS naive_infeasible_days,
       ROUND(SUM(CASE WHEN naive_feasible = 0 THEN naive_revenue - ordered_revenue END) / 1000, 0) AS overstated_k,
       SUM(ordered_revenue > perfect_revenue + 1)                                   AS ordered_above_milp_days,
       ROUND(SUM(CASE WHEN ordered_revenue > perfect_revenue + 1
                      THEN ordered_revenue - perfect_revenue END) / 1000, 1)        AS ordered_above_milp_k,
       SUM(ordered_revenue = 0)                                                     AS idle_days
FROM j;


-- @name: L3_11_sql_strategy_timing
-- 业务问题：SQL 有序版最常在几点开始充电、几点开始放电？
-- 知识点：JOIN 维度表取小时，GROUP BY 计数，ORDER BY 计数取前几名（求"众数"）。
-- 结果解读：如果最常见的充电时段是午间、放电是傍晚，和鸭子曲线一致；
--          项目里的固定规则（10–12 点充、17–19 点放）就是按这个规律设定的。
SELECT 'charge' AS action, i.hour, COUNT(*) AS days
FROM bt_sql_strategy_daily b JOIN dim_interval i ON i.slot = b.ordered_charge_slot
GROUP BY i.hour
UNION ALL
SELECT 'discharge', i.hour, COUNT(*)
FROM bt_sql_strategy_daily b JOIN dim_interval i ON i.slot = b.ordered_discharge_slot
GROUP BY i.hour
ORDER BY action, days DESC;


-- @name: L3_12_explain_no_index
-- 业务问题：查"电价超过 A$1,000 的区间"时，数据库是怎么找数据的？（不用索引）
-- 知识点：EXPLAIN QUERY PLAN 显示执行计划；NOT INDEXED 强制不用索引，用来对比。
-- 结果解读：detail 列显示 SCAN fact_price = 全表扫描，21 万行逐行检查 rrp。
--   实际输出：SCAN fact_price
EXPLAIN QUERY PLAN
SELECT COUNT(*) FROM fact_price NOT INDEXED WHERE rrp > 1000;


-- @name: L3_13_explain_with_index
-- 业务问题：有了 rrp 索引之后呢？
-- 知识点：索引是按 rrp 排好序的"目录"（B 树），可以直接跳到 rrp > 1000 的位置，只读需要的几百行；
--        COVERING INDEX 表示查询需要的列都在索引里，连原表都不用回。
-- 结果解读：detail 列显示 SEARCH fact_price USING COVERING INDEX idx_price_rrp (rrp>?)。
--          两种写法的实际耗时见 L3_14 / L3_15 运行时打印的毫秒数。
--   实际输出：SEARCH fact_price USING COVERING INDEX idx_price_rrp (rrp>?)
--   索引的代价：占用磁盘空间、插入数据时要同时更新索引，所以只给经常用于筛选、排序、连接的列建索引。
EXPLAIN QUERY PLAN
SELECT COUNT(*) FROM fact_price WHERE rrp > 1000;


-- @name: L3_14_timed_no_index
-- 业务问题：不用索引时，统计尖峰区间要多久？（看运行时打印的毫秒数）
-- 知识点：同一条查询，NOT INDEXED 强制全表扫描。
-- 结果解读：和 L3_15 结果相同，耗时不同。数据量越大，差距越明显（千万行级别时是秒和毫秒的区别）。
SELECT COUNT(*) AS spike_intervals, ROUND(SUM(rrp) / COUNT(*), 0) AS avg_spike_price
FROM fact_price NOT INDEXED
WHERE rrp > 1000;


-- @name: L3_15_timed_with_index
-- 业务问题：用索引时，统计尖峰区间要多久？
-- 知识点：同上，但让优化器自动使用 idx_price_rrp。
-- 结果解读：结果和 L3_14 完全相同，耗时明显更短。
SELECT COUNT(*) AS spike_intervals, ROUND(SUM(rrp) / COUNT(*), 0) AS avg_spike_price
FROM fact_price
WHERE rrp > 1000;
