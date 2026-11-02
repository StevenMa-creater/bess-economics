-- =====================================================================
-- L2 进阶：CTE（WITH）/ 窗口函数 ROW_NUMBER、RANK、LAG、LEAD / 滚动窗口 / 多表 JOIN
-- 窗口函数和 GROUP BY 的区别：GROUP BY 把多行压成一行；窗口函数不减少行数，
-- 而是给每一行"开一个窗口"，在窗口里做排名、取上一行、求滚动平均。
-- 运行：python run.py sql run --level 2
-- =====================================================================


-- @name: L2_01_daily_spread_by_month
-- 业务问题：每天"最贵 2 小时均价 − 最便宜 2 小时均价"是多少？按月看哪个季节套利空间最大？
-- 知识点：CTE（WITH 子句）把复杂查询拆成几步，每步像一张临时表；
--        ROW_NUMBER() OVER (PARTITION BY 交易日 ORDER BY 电价) 在每一天内部给 288 个区间排名；
--        rn <= 24 取出最便宜（或最贵）的 24 个区间 = 2 小时。
-- 结果解读：实际结果：2025 年下半年月均价差只有 A$130–212，明显低于 2024 年同期（A$245–930），
--          并且 2025 年 10–12 月没有一天价差超过 A$300：电池装机增加后，价差被"吃薄"了。
--          价差是 2 小时电池一天一次循环能赚到的"毛价差上限"（还没扣效率损失、也没考虑先充后放）。
--          月度平均价差比月均电价更能反映储能的收入潜力：电价高不一定价差大。
WITH ranked AS (                                   -- 第 1 步：每天内部排名
    SELECT trade_date, rrp,
           ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY rrp ASC)  AS rn_low,
           ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY rrp DESC) AS rn_high
    FROM fact_price
),
daily AS (                                         -- 第 2 步：每天算一个价差
    SELECT trade_date,
           AVG(CASE WHEN rn_high <= 24 THEN rrp END) - AVG(CASE WHEN rn_low <= 24 THEN rrp END) AS spread_2h
    FROM ranked
    GROUP BY trade_date
)
SELECT strftime('%Y-%m', trade_date)                           AS year_month,  -- 第 3 步：按月汇总
       ROUND(AVG(spread_2h), 1)                                AS avg_spread_2h,
       ROUND(MIN(spread_2h), 1)                                AS min_spread_2h,
       ROUND(MAX(spread_2h), 1)                                AS max_spread_2h,
       SUM(CASE WHEN spread_2h > 300 THEN 1 ELSE 0 END)        AS days_spread_over_300
FROM daily
GROUP BY year_month
ORDER BY year_month;


-- @name: L2_02_biggest_5min_jumps
-- 业务问题：电价在相邻两个 5 分钟区间之间最大跳了多少？发生在什么时候？
-- 知识点：LAG(rrp) OVER (ORDER BY trade_date, slot) 取"上一行"的值，相减得到逐区间变化；
--        窗口按 (日期, 区间) 排序，跨天也连续（前一天 23:55 → 当天 00:00）。
--        窗口函数的结果不能直接写在 WHERE 里，所以先放进 CTE，外层再筛选、排序。
-- 结果解读：几分钟内上涨上万澳元，说明电池必须提前满电待命，靠"看到涨价再充电"来不及；
--          这也是 5 分钟结算市场里快速响应资产（电池）比燃气机组更有优势的原因。
WITH chg AS (
    SELECT trade_date, slot, rrp,
           LAG(rrp) OVER (ORDER BY trade_date, slot) AS prev_rrp
    FROM fact_price
)
SELECT c.trade_date, i.start_time, ROUND(c.prev_rrp, 2) AS prev_rrp, ROUND(c.rrp, 2) AS rrp,
       ROUND(c.rrp - c.prev_rrp, 2) AS jump
FROM chg c
JOIN dim_interval i ON i.slot = c.slot
WHERE c.prev_rrp IS NOT NULL
ORDER BY ABS(c.rrp - c.prev_rrp) DESC
LIMIT 10;


-- @name: L2_03_daily_change_top10
-- 业务问题：日均电价"日环比"变化最大的是哪些天？
-- 知识点：先 GROUP BY 得到每日均价，再在结果上用 LAG 取前一天；(今天 − 昨天) / 昨天 = 日环比。
--        NULLIF(x, 0) 在 x = 0 时返回 NULL，避免除以 0 报错。
-- 结果解读：日均价突然翻几倍的日子，基本都是出现了尖峰价格；第二天往往又跌回去，
--          所以"用昨天电价预测今天"（persistence 策略）会在这些日子之后连续犯错。
WITH d AS (
    SELECT trade_date, AVG(rrp) AS avg_rrp
    FROM fact_price
    GROUP BY trade_date
),
d2 AS (
    SELECT trade_date, avg_rrp,
           LAG(avg_rrp) OVER (ORDER BY trade_date) AS prev_avg
    FROM d
)
SELECT trade_date, ROUND(prev_avg, 1) AS prev_day_avg, ROUND(avg_rrp, 1) AS day_avg,
       ROUND((avg_rrp - prev_avg) / NULLIF(prev_avg, 0) * 100, 0) AS dod_pct
FROM d2
WHERE prev_avg IS NOT NULL
ORDER BY ABS(avg_rrp - prev_avg) DESC
LIMIT 10;


-- @name: L2_04_monthly_mom
-- 业务问题：月均电价和上月比（月环比）、和下月比怎么变？和去年同月比（同比）呢？
-- 知识点：LAG(x, 1) 上一个月、LEAD(x, 1) 下一个月、LAG(x, 12) 去年同月；
--        一条查询里可以同时用多个窗口函数。
-- 结果解读：环比看短期季节性（冬季、夏季用电高峰），同比看结构性变化（光伏和储能装机增长）。
WITH m AS (
    SELECT strftime('%Y-%m', trade_date) AS year_month, AVG(rrp) AS avg_rrp
    FROM fact_price
    GROUP BY year_month
)
SELECT year_month,
       ROUND(avg_rrp, 1)                                                          AS avg_rrp,
       ROUND(LAG(avg_rrp, 1) OVER w, 1)                                           AS prev_month,
       ROUND(LEAD(avg_rrp, 1) OVER w, 1)                                          AS next_month,
       ROUND((avg_rrp / LAG(avg_rrp, 1) OVER w - 1) * 100, 0)                     AS mom_pct,
       ROUND((avg_rrp / LAG(avg_rrp, 12) OVER w - 1) * 100, 0)                    AS yoy_pct
FROM m
WINDOW w AS (ORDER BY year_month)       -- 命名窗口：多处使用同一个窗口定义时更简洁
ORDER BY year_month;


-- @name: L2_05_rolling_revenue
-- 业务问题：GBM 策略每日收入的 7 日、30 日滚动平均是多少？收入波动大不大？
-- 知识点：滚动窗口 ROWS BETWEEN 6 PRECEDING AND CURRENT ROW（当天 + 前 6 天 = 7 天）；
--        滚动标准差 = sqrt(平均(x²) − 平均(x)²)（SQLite 没有 STDEV 窗口函数，用公式算总体标准差）；
--        COUNT(*) OVER 同一窗口，数据不足 7 天 / 30 天时结果置为 NULL，避免开头几天的"半个窗口"误导。
-- 结果解读：30 日滚动平均反映季节性收入水平；滚动标准差越大，说明收入越依赖个别极端日，
--          这正是融资方关心的"收入不确定性"。完整 365 行见 outputs/sql/L2_05_rolling_revenue.csv。
WITH g AS (
    SELECT trade_date, revenue
    FROM fact_dispatch_daily
    WHERE strategy = 'gbm'
),
r AS (
    SELECT trade_date, revenue,
           AVG(revenue)            OVER w7  AS avg7,
           AVG(revenue * revenue)  OVER w7  AS sq7,
           COUNT(*)                OVER w7  AS n7,
           AVG(revenue)            OVER w30 AS avg30,
           AVG(revenue * revenue)  OVER w30 AS sq30,
           COUNT(*)                OVER w30 AS n30
    FROM g
    WINDOW w7  AS (ORDER BY trade_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW),
           w30 AS (ORDER BY trade_date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
)
SELECT trade_date,
       ROUND(revenue, 0)                                                         AS revenue,
       CASE WHEN n7  = 7  THEN ROUND(avg7, 0) END                                AS roll7_avg,
       CASE WHEN n7  = 7  THEN ROUND(sqrt(MAX(sq7  - avg7  * avg7,  0)), 0) END  AS roll7_std,
       CASE WHEN n30 = 30 THEN ROUND(avg30, 0) END                               AS roll30_avg,
       CASE WHEN n30 = 30 THEN ROUND(sqrt(MAX(sq30 - avg30 * avg30, 0)), 0) END  AS roll30_std
FROM r
ORDER BY trade_date;


-- @name: L2_06_monthly_capture_rate
-- 业务问题：GBM 策略每个月拿到了完美预知收入的百分之几（收益捕获率）？
-- 知识点：自连接（self join）：同一张表起两个别名 g 和 p，按交易日把两种策略放到同一行；
--        也可以用条件聚合 SUM(CASE WHEN strategy = 'gbm' …) 实现，两种写法结果相同。
-- 结果解读：捕获率高的月份，电价形态规律（每天都是午低晚高），预测容易；
--          捕获率低的月份通常有难以预测的尖峰，完美预知能吃到、预测策略吃不到。
SELECT strftime('%Y-%m', g.trade_date)              AS year_month,
       ROUND(SUM(p.revenue) / 1000, 0)              AS perfect_k,
       ROUND(SUM(g.revenue) / 1000, 0)              AS gbm_k,
       ROUND(SUM(g.revenue) / SUM(p.revenue) * 100, 1) AS capture_pct
FROM fact_dispatch_daily g
JOIN fact_dispatch_daily p
  ON p.trade_date = g.trade_date AND p.strategy = 'perfect'
WHERE g.strategy = 'gbm'
GROUP BY year_month
ORDER BY year_month;


-- @name: L2_07_monthly_strategy_rank
-- 业务问题：每个月四种策略的收入排名如何？
-- 知识点：RANK() OVER (PARTITION BY 月份 ORDER BY 收入 DESC)：每个月内部单独排名；
--        RANK 遇到并列会跳号（1, 1, 3），DENSE_RANK 不跳号（1, 1, 2），ROW_NUMBER 不允许并列（1, 2, 3）。
-- 结果解读：完美预知永远第 1；关键看 GBM 是否稳定排第 2，以及"昨日电价"和"固定时段"谁更好。
WITH m AS (
    SELECT strftime('%Y-%m', trade_date) AS year_month, strategy, SUM(revenue) AS revenue
    FROM fact_dispatch_daily
    GROUP BY year_month, strategy
)
SELECT year_month, strategy, ROUND(revenue / 1000, 0) AS revenue_k,
       RANK() OVER (PARTITION BY year_month ORDER BY revenue DESC) AS rank_in_month
FROM m
ORDER BY year_month, rank_in_month;


-- @name: L2_08_rank_summary
-- 业务问题：全年 12 个月里，每种策略分别有几个月排第 1、第 2、第 3、第 4？
-- 知识点：在窗口函数的结果上再 GROUP BY（窗口函数先算，所以要包一层 CTE）；条件计数把排名转成列。
-- 结果解读：实际结果：GBM 8 个月排第 2、4 个月排第 3；昨日电价 9 个月垫底。
--          如果"昨日电价 + 优化"多数月份排在"固定时段"后面，说明用质量差的预测去优化，
--          不如一个简单稳定的规则；预测质量是优化能否发挥作用的前提。
WITH m AS (
    SELECT strftime('%Y-%m', trade_date) AS year_month, strategy, SUM(revenue) AS revenue
    FROM fact_dispatch_daily
    GROUP BY year_month, strategy
),
r AS (
    SELECT strategy, RANK() OVER (PARTITION BY year_month ORDER BY revenue DESC) AS rk
    FROM m
)
SELECT s.name_zh AS strategy,
       SUM(rk = 1) AS months_1st, SUM(rk = 2) AS months_2nd, SUM(rk = 3) AS months_3rd, SUM(rk = 4) AS months_4th
FROM r
JOIN dim_strategy s ON s.strategy = r.strategy
GROUP BY s.strategy, s.name_zh
ORDER BY months_1st DESC, months_2nd DESC;


-- @name: L2_09_revenue_concentration
-- 业务问题：收入最高的前 5 天、前 10 天贡献了全年收入的百分之几？
-- 知识点：ROW_NUMBER() OVER (PARTITION BY 策略 ORDER BY 收入 DESC) 每种策略内部排名；
--        SUM(CASE WHEN rn <= 5 …) / SUM(全部) 一次算出多个占比。
-- 结果解读：实际结果：GBM 前 5 天占全年收入 31.5%，前 10 天占 35.2%。
--          前 5 天（全年 1.4% 的日子）就贡献了约三成收入，说明储能收入是"长尾分布"：
--          能否在极端价格日满电在线，比日常交易精不精更重要；也意味着可用率、检修计划非常关键。
WITH r AS (
    SELECT strategy, revenue,
           ROW_NUMBER() OVER (PARTITION BY strategy ORDER BY revenue DESC) AS rn
    FROM fact_dispatch_daily
)
SELECT s.name_zh                                                             AS strategy,
       ROUND(SUM(r.revenue) / 1e6, 2)                                        AS annual_revenue_m,
       ROUND(SUM(CASE WHEN rn <= 5  THEN r.revenue END) / SUM(r.revenue) * 100, 1) AS top5_pct,
       ROUND(SUM(CASE WHEN rn <= 10 THEN r.revenue END) / SUM(r.revenue) * 100, 1) AS top10_pct,
       ROUND(SUM(CASE WHEN rn <= 36 THEN r.revenue END) / SUM(r.revenue) * 100, 1) AS top10pct_days_pct
FROM r
JOIN dim_strategy s ON s.strategy = r.strategy
GROUP BY s.strategy, s.name_zh
ORDER BY annual_revenue_m DESC;
