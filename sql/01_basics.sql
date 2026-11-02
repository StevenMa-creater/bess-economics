-- =====================================================================
-- L1 基础：SELECT / WHERE / GROUP BY / ORDER BY / 聚合函数 / CASE WHEN
-- 数据：QLD1 2024–2025 年 5 分钟电价（fact_price），2025 年四种策略逐日回测（fact_dispatch_daily）
-- 运行：python run.py sql run --level 1
-- =====================================================================


-- @name: L1_01_monthly_price
-- 业务问题：每个月的平均电价、最高价、最低价是多少？哪个月最贵？
-- 知识点：GROUP BY 分组 + 聚合函数 AVG / MAX / MIN / COUNT；strftime 从日期里取年月；ROUND 保留小数。
-- 结果解读：均价看"电能量本身贵不贵"，最高价看"有没有尖峰"，最低价看"负电价有多深"。
--          月均价高的月份往往是因为少数尖峰区间把平均拉高，不一定每天都贵（L3 尖峰分析会验证）。
SELECT strftime('%Y-%m', trade_date)  AS year_month,
       COUNT(DISTINCT trade_date)     AS days,
       ROUND(AVG(rrp), 2)             AS avg_rrp,
       ROUND(MAX(rrp), 2)             AS max_rrp,
       ROUND(MIN(rrp), 2)             AS min_rrp
FROM fact_price
GROUP BY year_month
ORDER BY year_month;


-- @name: L1_02_negative_price_share
-- 业务问题：负电价时段占多少？一年大概有多少小时电价是负的？
-- 知识点：条件计数 SUM(CASE WHEN … THEN 1 ELSE 0 END)；AVG(条件) 直接得到占比；
--        每个区间 5 分钟 = 1/12 小时，所以小时数 = 区间数 / 12。
-- 结果解读：实际结果：负电价占比从 2024 年 14.8%（约 1,299 小时）升到 2025 年 20.3%（约 1,782 小时）。
--          负电价是储能的"免费原料"，充电还能拿钱。占比越高，套利空间越大；
--          占比逐年上升说明光伏增长快于储能，午间电越来越过剩。
SELECT strftime('%Y', trade_date)                          AS year,
       COUNT(*)                                            AS intervals,
       SUM(CASE WHEN rrp < 0 THEN 1 ELSE 0 END)            AS negative_intervals,
       ROUND(AVG(CASE WHEN rrp < 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS negative_pct,
       ROUND(SUM(CASE WHEN rrp < 0 THEN 1 ELSE 0 END) / 12.0, 0)    AS negative_hours,
       ROUND(AVG(CASE WHEN rrp < 0 THEN rrp END), 2)       AS avg_negative_price
FROM fact_price
GROUP BY year
ORDER BY year;


-- @name: L1_03_hourly_profile
-- 业务问题：一天中每个小时的平均电价（"鸭子曲线"）是什么样？2025 年和 2024 年比有什么变化？
-- 知识点：整数除法 slot / 12 得到小时；条件聚合 AVG(CASE WHEN year = … THEN rrp END) 把两年并排成两列（行转列）；
--        MIN(MAX(rrp, -500), 1000) 把极端值截断，减少尖峰对平均值的影响。
-- 结果解读：午间（10–15 点）价格最低甚至为负，傍晚（17–20 点）最高，这个"低—高"就是储能每天的套利来源。
--          截断均价和普通均价差得越多，说明那个小时的平均值被少数尖峰拉高得越厉害。
SELECT slot / 12                                                        AS hour,
       ROUND(AVG(CASE WHEN trade_date < '2025-01-01'  THEN rrp END), 1) AS avg_2024,
       ROUND(AVG(CASE WHEN trade_date >= '2025-01-01' THEN rrp END), 1) AS avg_2025,
       ROUND(AVG(CASE WHEN trade_date >= '2025-01-01' THEN MIN(MAX(rrp, -500), 1000) END), 1) AS clipped_avg_2025
FROM fact_price
GROUP BY hour
ORDER BY hour;


-- @name: L1_04_strategy_annual_revenue
-- 业务问题：四种调度策略 2025 年分别赚了多少钱？
-- 知识点：JOIN 维度表拿中文名；SUM 汇总；单位换算（整块 100 MW 电池的 A$ → 千 A$/MW/年）；ORDER BY … DESC 降序。
-- 结果解读：完美预知是理论上限，其他策略和它的比值就是"收益捕获率"（L2 会按月算）。
--          固定时段规则循环次数最少，但收入并不是最低，说明"多交易"不等于"多赚钱"。
SELECT s.name_zh                               AS strategy,
       COUNT(*)                                AS days,
       ROUND(SUM(f.revenue) / 100 / 1000, 1)   AS revenue_k_per_mw,
       ROUND(SUM(f.cycles), 0)                 AS cycles,
       ROUND(SUM(f.discharge_mwh), 0)          AS discharge_mwh
FROM fact_dispatch_daily f
JOIN dim_strategy s ON s.strategy = f.strategy
GROUP BY s.strategy, s.name_zh
ORDER BY revenue_k_per_mw DESC;


-- @name: L1_05_price_bands
-- 业务问题：电价落在各个价格区间的时间各占多少？尖峰（> A$1,000）有多少？
-- 知识点：CASE WHEN 分档；按分档 GROUP BY；用一个排序列（band_order）控制显示顺序；
--        COUNT(*) * 100.0 / (SELECT COUNT(*) …) 用子查询算占比（乘 100.0 而不是 100，避免整数除法得到 0）。
-- 结果解读：实际结果：0–300 之间占约 81% 的时间；> A$1,000 的尖峰只有 381 个区间（0.18%，约 32 小时）。
--          尖峰区间占比很小但金额极大；
--          这就是为什么储能收入高度集中在少数几天（L2 收益集中度会量化）。
SELECT CASE WHEN rrp < 0     THEN 1
            WHEN rrp < 50    THEN 2
            WHEN rrp < 100   THEN 3
            WHEN rrp < 300   THEN 4
            WHEN rrp <= 1000 THEN 5
            ELSE 6 END                                         AS band_order,
       CASE WHEN rrp < 0     THEN '负电价 (<0)'
            WHEN rrp < 50    THEN '低价 (0–50)'
            WHEN rrp < 100   THEN '中价 (50–100)'
            WHEN rrp < 300   THEN '较高 (100–300)'
            WHEN rrp <= 1000 THEN '高价 (300–1000)'
            ELSE '尖峰 (>1000)' END                            AS price_band,
       COUNT(*)                                                AS intervals,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM fact_price), 2) AS pct_of_time,
       ROUND(COUNT(*) / 12.0, 0)                               AS hours,
       ROUND(AVG(rrp), 1)                                      AS avg_rrp
FROM fact_price
GROUP BY band_order, price_band
ORDER BY band_order;


-- @name: L1_06_top10_prices
-- 业务问题：两年里最贵的 10 个 5 分钟区间是什么时候？
-- 知识点：JOIN 维度表把 slot 翻译成钟点；ORDER BY … DESC + LIMIT 取前 N。
-- 结果解读：最高价大多在傍晚（也有冬季清晨，如 2024-07-30 05:40），而且往往同一天连续出现，对应极端天气下的供应紧张；
--          电池能不能在这几个区间满电放出，决定了一年收入的相当一部分。
SELECT p.trade_date, i.start_time, d.weekday_name, ROUND(p.rrp, 2) AS rrp, ROUND(p.demand, 0) AS demand_mw
FROM fact_price p
JOIN dim_interval i ON i.slot = p.slot
JOIN dim_date d     ON d.trade_date = p.trade_date
ORDER BY p.rrp DESC
LIMIT 10;
