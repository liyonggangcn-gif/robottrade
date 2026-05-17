"""
Direct-SQLite AI Model: Train LightGBM on SQLite data and save predictions.

纯 Python + LightGBM 方案，不依赖 Qlib。
直接从 SQLite 读取数据，手动计算技术指标，训练 LightGBM 模型。

Usage:
    python scripts/run_ai_model.py

Prerequisites:
    pip install lightgbm pandas numpy scikit-learn
"""

import sys
import os
import io

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Windows 控制台 UTF-8
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("run_ai_model")

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import lightgbm as lgb
from sklearn.preprocessing import MinMaxScaler

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


def load_stock_data():
    """
    加载股票数据（近1年，分批读取避免MySQL大结果集断连）

    Returns:
        pd.DataFrame: ts_code, trade_date, OHLCV + pe_ttm, total_mv, roe, gpr, netprofit_yoy
    """
    print("\n[步骤 1] 加载股票数据...")

    # 1年数据足够LightGBM训练，同时避免MySQL传输过大导致断连
    since_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

    # 先获取该时间段内所有交易日期列表
    dates_sql = "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date >= ? ORDER BY trade_date"
    dates_df = DBUtils.query_df(dates_sql, params=(since_date,))
    if dates_df.empty:
        raise ValueError(f"stock_daily 中 {since_date} 之后无数据")
    trade_dates = dates_df['trade_date'].astype(str).tolist()
    print(f"    - 共 {len(trade_dates)} 个交易日，分批加载...")

    # 按月批次加载，每批 ~20 个交易日，避免单次查询结果集过大
    col_sql = """
    SELECT
        sd.ts_code, sd.trade_date,
        sd.open, sd.high, sd.low, sd.close, sd.vol,
        COALESCE(sd.amount, 0) as amount,
        COALESCE(sd.total_mv, 0)     as total_mv,
        sd.pe_ttm, sd.roe, sd.gpr, sd.netprofit_yoy
    FROM stock_daily sd
    WHERE sd.trade_date >= ? AND sd.trade_date <= ?
    ORDER BY sd.ts_code, sd.trade_date
    """
    BATCH = 20  # 每批交易日数
    chunks = []
    for i in range(0, len(trade_dates), BATCH):
        batch = trade_dates[i:i + BATCH]
        chunk = DBUtils.query_df(col_sql, params=(batch[0], batch[-1]))
        chunks.append(chunk)
        if (i // BATCH) % 5 == 0:
            print(f"    - 已加载 {i + len(batch)}/{len(trade_dates)} 个交易日...")

    df = pd.concat(chunks, ignore_index=True)
    print(f"    - 读取了 {len(df)} 条记录，股票数量: {df['ts_code'].nunique()}")
    print(f"    - 日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")

    df['trade_date'] = pd.to_datetime(df['trade_date'])
    # 只填充 amount/total_mv（无则为0合理），其他财务字段保持 NaN
    # LightGBM 原生支持 NaN，比填 0 假信号更好
    for col in ['amount', 'total_mv']:
        df[col] = df[col].fillna(0)
    # 财务字段全为 NULL 时 pandas 推断为 object dtype，LightGBM 不接受
    # 强制转为 float（NaN 保留）
    for col in ['pe_ttm', 'roe', 'gpr', 'netprofit_yoy']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # 诊断：显示财务字段的缺失率
    fin_cols = ['pe_ttm', 'roe', 'gpr', 'netprofit_yoy']
    print("    [财务字段覆盖率诊断]")
    for c in fin_cols:
        if c in df.columns:
            null_pct = df[c].isna().mean() * 100
            zero_pct = (df[c] == 0).mean() * 100
            print(f"      {c:<18}: NaN={null_pct:.1f}%  Zero={zero_pct:.1f}%")

    return df


def calculate_technical_features(df):
    """
    手动计算技术指标特征
    
    Args:
        df: 包含 OHLCV 数据的 DataFrame
        
    Returns:
        pd.DataFrame: 添加了技术指标的 DataFrame
    """
    print("\n[步骤 2] 计算技术指标特征...")
    
    # 按股票分组计算
    features_list = []
    
    for ts_code, group in df.groupby('ts_code'):
        group = group.sort_values('trade_date').copy()
        
        # 1. 价格动量特征
        group['return_1d'] = group['close'].pct_change(1)
        group['return_5d'] = group['close'].pct_change(5)
        group['return_10d'] = group['close'].pct_change(10)
        group['return_20d'] = group['close'].pct_change(20)
        
        # 2. 移动平均线
        group['ma5'] = group['close'].rolling(5).mean()
        group['ma10'] = group['close'].rolling(10).mean()
        group['ma20'] = group['close'].rolling(20).mean()
        group['ma60'] = group['close'].rolling(60).mean()
        
        # 3. 价格位置指标
        group['close_to_ma5'] = group['close'] / group['ma5'] - 1
        group['close_to_ma20'] = group['close'] / group['ma20'] - 1
        group['ma5_to_ma20'] = group['ma5'] / group['ma20'] - 1
        
        # 4. 波动率
        group['volatility_5d'] = group['return_1d'].rolling(5).std()
        group['volatility_20d'] = group['return_1d'].rolling(20).std()
        
        # 5. RSI (Relative Strength Index)
        delta = group['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        group['rsi_14'] = 100 - (100 / (1 + rs))
        
        # 6. 动量振荡器 (MOM)
        group['mom_10'] = group['close'] - group['close'].shift(10)
        group['mom_20'] = group['close'] - group['close'].shift(20)
        
        # 7. 变化率 (ROC)
        group['roc_10'] = (group['close'] / group['close'].shift(10) - 1) * 100
        group['roc_20'] = (group['close'] / group['close'].shift(20) - 1) * 100
        
        # 8. 成交量特征
        group['vol_ma5'] = group['vol'].rolling(5).mean()
        group['vol_ma20'] = group['vol'].rolling(20).mean()
        group['vol_ratio'] = group['vol'] / group['vol_ma20']
        
        # 9. 价格范围
        group['high_low_ratio'] = (group['high'] - group['low']) / group['close']

        # 10. 基本面特征 (已有)
        # pe_ttm, pb, total_mv, turnover_rate

        # 11. 标签：下一日收益率
        group['label'] = group['close'].shift(-1) / group['close'] - 1

        # 12. 换手率代理（成交额/总市值，两者均为万元）
        group['turnover_approx'] = group['amount'] / group['total_mv'].replace(0, np.nan)

        # 13. 换手率变化（今日换手率 / 5日均换手率，衡量资金涌入异常）
        group['turnover_ratio'] = group['turnover_approx'] / group['turnover_approx'].rolling(5).mean()

        # 14. 近5日换手率均值（绝对流动性水平）
        group['turnover_ma5'] = group['turnover_approx'].rolling(5).mean()

        # 15. 20日最大回撤（衡量下行风险）
        roll_max = group['close'].rolling(20, min_periods=5).max()
        group['drawdown_20'] = (group['close'] / roll_max - 1)

        # 16. 近10日是否过热（价格涨幅，用于防止追涨）
        group['gain_10d'] = group['close'] / group['close'].shift(10) - 1

        # 17. 价格在52周高低点中的位置（0=52周低, 1=52周高）
        roll_max_52 = group['close'].rolling(250, min_periods=20).max()
        roll_min_52 = group['close'].rolling(250, min_periods=20).min()
        group['price_pos_52w'] = (group['close'] - roll_min_52) / (roll_max_52 - roll_min_52 + 1e-9)

        features_list.append(group)

    result = pd.concat(features_list, ignore_index=True)

    # 18. 60日Beta（相对全市场等权收益率）
    print("    - 计算 Beta 因子...")
    try:
        mkt_ret = result.groupby('trade_date')['return_1d'].mean().rename('mkt_ret')
        result = result.merge(mkt_ret.reset_index(), on='trade_date', how='left')

        # pandas 3.0 breaking change: groupby.apply excludes grouping column from
        # the group argument, dropping ts_code. Use explicit loop instead.
        result['beta_60'] = np.nan
        for _ts, _grp in result.groupby('ts_code'):
            _grp = _grp.sort_values('trade_date')
            stock_r = _grp['return_1d'].values
            mkt_r = _grp['mkt_ret'].values
            betas = []
            for i in range(len(_grp)):
                lo = max(0, i - 60)
                s = stock_r[lo:i+1]
                m = mkt_r[lo:i+1]
                if len(s) < 20:
                    betas.append(np.nan)
                else:
                    cov = np.cov(s, m)
                    betas.append(cov[0, 1] / (cov[1, 1] + 1e-9))
            result.loc[_grp.index, 'beta_60'] = betas
        result = result.drop(columns=['mkt_ret'], errors='ignore')
        print("    - Beta 因子计算完成")
    except Exception as e:
        result['beta_60'] = np.nan
        print(f"    - [WARN] Beta 计算失败: {e}")

    # 仅移除计算技术指标造成的 NaN 行（如 ma60 窗口不足的早期行）
    # 财务字段（pe_ttm/roe/gpr/netprofit_yoy）允许 NaN，LightGBM 原生支持
    # label 允许 NaN（最新日期无下一日价格），留给 predict_all_dates 使用
    SKIP_COLS = {'label', 'ts_code', 'trade_date', 'mkt_ret',
                 'pe_ttm', 'roe', 'gpr', 'netprofit_yoy', 'amount',
                 'total_mv', 'turnover_approx', 'turnover_ratio', 'turnover_ma5'}
    technical_cols = [c for c in result.columns if c not in SKIP_COLS]
    result = result.dropna(subset=technical_cols)

    print(f"    - 生成了 {len(result)} 条特征数据")
    print(f"    - 特征维度: {result.shape[1]}")
    
    return result


def prepare_train_test_split(df, split_date='2024-01-01'):
    """
    划分训练集和测试集
    
    Args:
        df: 包含特征和标签的 DataFrame
        split_date: 分割日期
        
    Returns:
        tuple: (X_train, y_train, X_test, y_test, test_info)
    """
    print(f"\n[步骤 3] 划分训练集和测试集 (分割日期: {split_date})...")
    
    # 特征列表（pb 未在 MySQL stock_daily 中，已移除）
    feature_cols = [
        'open', 'high', 'low', 'close', 'vol', 'amount',
        'pe_ttm', 'total_mv', 'roe', 'gpr', 'netprofit_yoy',
        'return_1d', 'return_5d', 'return_10d', 'return_20d',
        'ma5', 'ma10', 'ma20', 'ma60',
        'close_to_ma5', 'close_to_ma20', 'ma5_to_ma20',
        'volatility_5d', 'volatility_20d',
        'rsi_14', 'mom_10', 'mom_20', 'roc_10', 'roc_20',
        'vol_ma5', 'vol_ma20', 'vol_ratio',
        'high_low_ratio',
        'turnover_approx', 'turnover_ratio', 'turnover_ma5', 'drawdown_20', 'gain_10d', 'price_pos_52w',
        'beta_60'
    ]
    
    # 确保所有特征列都存在
    feature_cols = [col for col in feature_cols if col in df.columns]
    
    split_date = pd.to_datetime(split_date)

    # label=NaN 的行（各股最新日期）只用于预测，不能用于训练/评估
    df_labeled = df.dropna(subset=['label'])

    train_df = df_labeled[df_labeled['trade_date'] < split_date].copy()
    test_df = df_labeled[df_labeled['trade_date'] >= split_date].copy()
    
    X_train = train_df[feature_cols]
    y_train = train_df['label']
    
    X_test = test_df[feature_cols]
    y_test = test_df['label']
    
    test_info = test_df[['ts_code', 'trade_date', 'close']].copy()
    
    print(f"    - 训练集: {len(X_train)} 条 ({train_df['trade_date'].min()} ~ {train_df['trade_date'].max()})")
    print(f"    - 测试集: {len(X_test)} 条 ({test_df['trade_date'].min()} ~ {test_df['trade_date'].max()})")
    print(f"    - 特征数量: {len(feature_cols)}")
    
    return X_train, y_train, X_test, y_test, test_info, feature_cols


def train_lightgbm_model(X_train, y_train, X_test, y_test):
    """
    训练 LightGBM 模型
    
    Args:
        X_train, y_train: 训练数据
        X_test, y_test: 测试数据
        
    Returns:
        lgb.Booster: 训练好的模型
    """
    print("\n[步骤 4] 训练 LightGBM 模型...")
    
    # LightGBM 参数
    params = {
        'objective': 'regression',
        'metric': 'rmse',
        'boosting_type': 'gbdt',
        'num_leaves': 31,
        'learning_rate': 0.05,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'verbose': -1,
        'seed': 42,
    }
    
    # 创建 LightGBM 数据集
    train_data = lgb.Dataset(X_train, label=y_train)
    test_data = lgb.Dataset(X_test, label=y_test, reference=train_data)
    
    # 训练模型
    print("    - 开始训练...")
    model = lgb.train(
        params,
        train_data,
        num_boost_round=200,
        valid_sets=[train_data, test_data],
        valid_names=['train', 'test'],
        callbacks=[
            lgb.early_stopping(stopping_rounds=20),
            lgb.log_evaluation(period=50)
        ]
    )
    
    print(f"    - 训练完成，最佳迭代次数: {model.best_iteration}")
    
    # 评估模型
    y_pred_train = model.predict(X_train)
    y_pred_test = model.predict(X_test)
    
    train_corr = np.corrcoef(y_train, y_pred_train)[0, 1]
    test_corr = np.corrcoef(y_test, y_pred_test)[0, 1]
    
    print(f"    - 训练集相关性: {train_corr:.4f}")
    print(f"    - 测试集相关性: {test_corr:.4f}")
    
    return model


def predict_latest_date(df, model, feature_cols):
    """
    预测最新日期的股票评分（兼容旧调用接口，内部委托给 predict_all_dates）

    Returns:
        pd.DataFrame: 仅含最新交易日的 ts_code, trade_date, ai_score
    """
    all_preds = predict_all_dates(df, model, feature_cols)
    if all_preds.empty:
        return all_preds
    latest_date = all_preds['trade_date'].max()
    return all_preds[all_preds['trade_date'] == latest_date].reset_index(drop=True)


def predict_all_dates(df, model, feature_cols):
    """
    对 df 中的所有日期逐日预测评分，并按日截面归一化到 0-1。

    历史日期的预测让回测可以使用真实 AI 分数（而非价格动量代理）。

    Args:
        df: 包含特征的完整 DataFrame（含 trade_date 列，datetime 类型）
        model: 训练好的 LightGBM 模型
        feature_cols: 特征列列表

    Returns:
        pd.DataFrame: 包含 ts_code, trade_date(str), ai_score
    """
    print("\n[步骤 5] 预测所有日期的股票评分（按日截面归一化）...")

    scaler = MinMaxScaler()
    all_results = []

    sorted_dates = sorted(df['trade_date'].unique())
    print(f"    - 共 {len(sorted_dates)} 个交易日需要预测")

    for i, dt in enumerate(sorted_dates):
        day_df = df[df['trade_date'] == dt].copy()
        if len(day_df) == 0:
            continue
        X_day = day_df[feature_cols]
        raw_preds = model.predict(X_day)
        if len(raw_preds) < 2:
            norm_preds = raw_preds.copy()
        else:
            norm_preds = scaler.fit_transform(raw_preds.reshape(-1, 1)).flatten()
        date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)
        all_results.append(pd.DataFrame({
            'trade_date': date_str,
            'ts_code': day_df['ts_code'].values,
            'ai_score': norm_preds,
        }))
        if (i + 1) % 20 == 0:
            print(f"    - 已预测 {i + 1}/{len(sorted_dates)} 个交易日...")

    if not all_results:
        print("    [WARN] 没有可预测的数据")
        return pd.DataFrame(columns=['trade_date', 'ts_code', 'ai_score'])

    result = pd.concat(all_results, ignore_index=True)
    latest = result['trade_date'].max()
    print(f"    - 预测完成，共 {len(result)} 条记录，最新日期: {latest}")
    return result


def _ensure_ai_predictions_schema():
    """确保 ai_predictions 表有正确的 schema（trade_date / ts_code / ai_score）。
    若旧表 schema 不兼容（如 pred_date/pred_score），删除并重建。
    """
    try:
        DBUtils.query_df("SELECT trade_date, ts_code, ai_score FROM ai_predictions LIMIT 1")
    except Exception:
        print("    [INFO] ai_predictions 表结构不兼容，重建...")
        DBUtils.execute("DROP TABLE IF EXISTS ai_predictions")
        DBUtils.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date VARCHAR(10) NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                ai_score FLOAT
            )
        """)
        print("    [INFO] ai_predictions 表已重建")


def save_predictions_to_db(predictions_df):
    """
    保存预测结果到 ai_predictions 表（支持多个交易日）

    Args:
        predictions_df: 包含 trade_date, ts_code, ai_score 的 DataFrame
    """
    print("\n[步骤 6] 保存预测结果到数据库...")

    if predictions_df.empty:
        print("    [WARN] 没有预测结果需要保存")
        return

    # 确保表结构正确（兼容 MySQL 旧 schema）
    _ensure_ai_predictions_schema()

    # 查询已存在的日期，跳过已保存的历史日期（只更新最新日期）
    latest_date = predictions_df['trade_date'].max()
    try:
        existing = DBUtils.query_df(
            "SELECT DISTINCT trade_date FROM ai_predictions WHERE trade_date >= ?",
            params=(predictions_df['trade_date'].min(),)
        )
        existing_dates = set(existing['trade_date'].astype(str).tolist()) if not existing.empty else set()
    except Exception:
        existing_dates = set()

    # 最新日期始终重新保存；历史日期仅在不存在时保存
    to_save = predictions_df[
        (predictions_df['trade_date'] == latest_date) |
        (~predictions_df['trade_date'].isin(existing_dates))
    ].copy()

    if to_save.empty:
        print(f"    - 所有历史日期已存在，无需重复写入")
        return

    dates_to_delete = to_save['trade_date'].unique().tolist()
    records = [
        (str(r['trade_date']), str(r['ts_code']), float(r['ai_score']))
        for _, r in to_save.iterrows()
    ]

    # 用 cursor executemany 写入（兼容 MySQL / SQLite）
    BATCH = 500
    with DBUtils.get_conn() as conn:
        cursor = conn.cursor()
        for d in dates_to_delete:
            cursor.execute("DELETE FROM ai_predictions WHERE trade_date = ?", (d,))
        for i in range(0, len(records), BATCH):
            cursor.executemany(
                "INSERT INTO ai_predictions (trade_date, ts_code, ai_score) VALUES (?, ?, ?)",
                records[i:i + BATCH]
            )

    print(f"    - 已保存 {len(to_save)} 条预测记录，覆盖 {len(dates_to_delete)} 个交易日")
    print(f"    - 日期范围: {to_save['trade_date'].min()} ~ {latest_date}")

    # 显示最新日期 Top 10
    top10 = to_save[to_save['trade_date'] == latest_date].nlargest(10, 'ai_score')
    if not top10.empty:
        print(f"\n    [最新日期 {latest_date} Top 10 AI评分]")
        for _, row in top10.iterrows():
            print(f"      {row['ts_code']: <12} AI评分: {row['ai_score']:.4f}")


def main():
    """主函数：完整的 AI 模型训练和预测流程"""
    
    print("=" * 70)
    print("  Direct-SQLite AI Model - LightGBM 训练与预测")
    print("=" * 70)
    
    try:
        # 1. 加载数据
        df = load_stock_data()
        
        # 2. 计算技术指标
        df_features = calculate_technical_features(df)
        
        # 3. 划分训练集和测试集（用最近3个月做测试集）
        from datetime import datetime, timedelta
        split_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        X_train, y_train, X_test, y_test, test_info, feature_cols = \
            prepare_train_test_split(df_features, split_date=split_date)
        
        # 4. 训练模型
        model = train_lightgbm_model(X_train, y_train, X_test, y_test)

        # 4b. 因子重要性诊断
        importance = model.feature_importance(importance_type='gain')
        fi_df = pd.DataFrame({'feature': feature_cols, 'gain': importance})
        fi_df = fi_df.sort_values('gain', ascending=False)
        print("\n[因子重要性 Top 20 (gain)]")
        for _, r in fi_df.head(20).iterrows():
            bar = '█' * min(int(r['gain'] / fi_df['gain'].max() * 20), 20)
            print(f"  {r['feature']:<22} {bar} {r['gain']:.1f}")
        fin_features = ['pe_ttm', 'roe', 'gpr', 'netprofit_yoy']
        fin_rows = fi_df[fi_df['feature'].isin(fin_features)]
        if not fin_rows.empty:
            print("\n[财务因子重要性（可能受缺失数据影响）]")
            for _, r in fin_rows.iterrows():
                null_pct = df_features[r['feature']].isna().mean() * 100 if r['feature'] in df_features else 0
                print(f"  {r['feature']:<22} gain={r['gain']:.1f}  NaN率={null_pct:.1f}%")

        # 5. 预测所有日期（历史积累 + 最新日期），用于回测与当日选股
        predictions = predict_all_dates(df_features, model, feature_cols)

        # 6. 保存到数据库（历史日期只补缺，最新日期始终刷新）
        save_predictions_to_db(predictions)

        print("\n" + "=" * 70)
        print("  ✅ AI 模型训练和预测完成！")
        print("=" * 70)
        
        return True
        
    except Exception as e:
        print(f"\n[ERROR] AI 模型运行失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
