"""次日电价预测（D-1 收盘时预测 D 日 288 个区间）。

只用 D-1 及更早的信息，严格避免"未来数据泄漏"。按月滚动重训（walk-forward）。

模型：
- persistence：D 日 = D-1 日同时段电价（最朴素的基准）
- gbm：梯度提升树（scikit-learn HistGradientBoostingRegressor），特征全部为滞后量 + 日历变量
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

CLIP = (-500.0, 1000.0)   # 训练目标截断：尖峰不可预测，留在目标里只会拉偏模型


def features(price: pd.DataFrame, demand: pd.DataFrame | None = None) -> pd.DataFrame:
    """price/demand 为 交易日 × 288 矩阵。返回长表（每行 = 某日某区间），全部特征只用 D-1 及更早数据。"""
    P = price.sort_index()
    days = P.index
    lag1 = P.shift(1)
    lag2 = P.shift(2)
    lag7 = P.shift(7)
    mean7 = sum(P.shift(k) for k in range(1, 8)) / 7
    clipped = P.clip(*CLIP)
    med7 = pd.concat([clipped.shift(k) for k in range(1, 8)]).groupby(level=0).median().reindex(days)

    def long(m: pd.DataFrame, name: str) -> pd.Series:
        s = m.stack(future_stack=True)
        s.name = name
        return s

    cols = [long(lag1, "lag1"), long(lag2, "lag2"), long(lag7, "lag7"), long(mean7, "mean7"), long(med7, "med7")]
    day_stats = pd.DataFrame({"d1_mean": P.shift(1).mean(axis=1), "d1_std": P.shift(1).std(axis=1),
                              "d1_min": P.shift(1).min(axis=1), "d1_max": P.shift(1).max(axis=1),
                              "d1_neg_share": (P.shift(1) < 0).mean(axis=1)}, index=days)
    if demand is not None and demand.notna().any().any():
        D = demand.sort_index()
        cols += [long(D.shift(1), "dem_lag1"), long(D.shift(7), "dem_lag7")]
        day_stats["dem_d1_mean"] = D.shift(1).mean(axis=1)
    X = pd.concat(cols, axis=1)
    X.index.names = ["trade_date", "slot"]
    X = X.join(day_stats, on="trade_date")
    idx = X.index.to_frame(index=False)
    slot = idx.slot.to_numpy()
    td = pd.to_datetime(idx.trade_date)
    X["slot_sin"] = np.sin(2 * np.pi * slot / 288)
    X["slot_cos"] = np.cos(2 * np.pi * slot / 288)
    X["slot"] = slot
    X["dow"] = td.dt.dayofweek.to_numpy()
    X["doy_sin"] = np.sin(2 * np.pi * td.dt.dayofyear.to_numpy() / 365.25)
    X["doy_cos"] = np.cos(2 * np.pi * td.dt.dayofyear.to_numpy() / 365.25)
    y = long(P, "y").reindex(X.index)
    X["y"] = y
    return X


def persistence(price: pd.DataFrame) -> pd.DataFrame:
    return price.sort_index().shift(1)


def walk_forward_gbm(price: pd.DataFrame, demand: pd.DataFrame | None, test_start: pd.Timestamp,
                     min_history_days: int = 60, seed: int = 0) -> pd.DataFrame:
    """从 test_start 起按月重训，返回 交易日 × 288 的预测矩阵（只含测试期）。"""
    F = features(price, demand).dropna()
    dates = F.index.get_level_values("trade_date")
    test_days = price.index[price.index >= test_start]
    months = pd.PeriodIndex(test_days, freq="M").unique()
    preds = []
    feat_cols = [c for c in F.columns if c != "y"]
    for m in months:
        m_start = m.start_time
        train = F[dates < m_start]
        if train.index.get_level_values("trade_date").nunique() < min_history_days:
            continue
        model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, max_leaf_nodes=48,
                                              min_samples_leaf=60, l2_regularization=1.0, random_state=seed)
        model.fit(train[feat_cols], train.y.clip(*CLIP))
        test = F[(dates >= m_start) & (dates <= m.end_time)]
        if test.empty:
            continue
        p = pd.Series(model.predict(test[feat_cols]), index=test.index, name="pred")
        preds.append(p)
    pred = pd.concat(preds).unstack("slot").sort_index()
    pred.columns.name = None
    return pred


def error_metrics(pred: pd.DataFrame, actual: pd.DataFrame) -> dict:
    a = actual.loc[pred.index].to_numpy()
    p = pred.to_numpy()
    mask = ~np.isnan(p)
    err = p[mask] - a[mask]
    clip_a = np.clip(a[mask], *CLIP)
    # 形状指标：预测与实际的日内排序相关性（调度只关心"哪段贵哪段便宜"）
    rank_corr = []
    for i in range(len(pred)):
        pi, ai = pred.iloc[i].to_numpy(), actual.loc[pred.index[i]].to_numpy()
        if np.isnan(pi).any():
            continue
        rank_corr.append(pd.Series(pi).corr(pd.Series(ai), method="spearman"))
    return dict(mae=float(np.mean(np.abs(err))), mae_clipped=float(np.mean(np.abs(p[mask] - clip_a))),
                rmse=float(np.sqrt(np.mean(err ** 2))), intraday_rank_corr=float(np.nanmean(rank_corr)))
