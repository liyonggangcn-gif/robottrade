"""
多数据源适配器（Multi-Source Adapter）

统一封装 A股免费数据源，提供与 Tushare 一致的接口。
支持自动降级：Tushare → AKShare → eFinance → Baostock → Sina

数据源覆盖：
  ┌──────────┬──────────────┬──────────────┬─────────────┐
  │ 数据     │ AKShare      │ eFinance     │ Baostock    │
  ├──────────┼──────────────┼──────────────┼─────────────┤
  │ 日线行情  │ ✅ 全市场     │ ✅ 全市场     │ ✅ 全市场    │
  │ 实时行情  │ ✅ 新浪源     │ ✅ 东财源     │ ❌          │
  │ 股票列表  │ ✅ 东财源     │ ✅ 东财源     │ ✅          │
  │ 财务数据  │ ✅ 三表       │ ❌           │ ✅ 三表     │
  │ ETF行情   │ ✅ 全量       │ ✅ 全量       │ ❌          │
  │ ETF历史   │ ✅ 全量       │ ❌           │ ❌          │
  │ 可转债    │ ✅ 集思录     │ ❌           │ ❌          │
  │ 指数成分  │ ✅ 全指数     │ ❌           │ ❌          │
  │ 板块概念  │ ✅ 东财源     │ ❌           │ ❌          │
  │ 北向资金  │ ✅ 东财源     │ ❌           │ ❌          │
  │ 龙虎榜    │ ✅ 东财源     │ ❌           │ ❌          │
  │ 宏观经济  │ ✅ 统计局     │ ❌           │ ❌          │
  └──────────┴──────────────┴──────────────┴─────────────┘
"""

import os
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

# 禁用代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# 延迟导入
ak = None
efinance = None
baostock = None

try:
    import akshare as ak
except ImportError:
    logger.warning("[DataSources] akshare 未安装，部分功能不可用")

try:
    import efinance
except ImportError:
    logger.warning("[DataSources] efinance 未安装")

try:
    import baostock as bs
    _bs_logged_in = False
except ImportError:
    logger.warning("[DataSources] baostock 未安装")


def _ensure_baostock():
    """确保 baostock 已登录"""
    global _bs_logged_in
    if baostock is None:
        return False
    if not _bs_logged_in:
        try:
            lg = bs.login()
            _bs_logged_in = (lg.error_code == '0')
            if _bs_logged_in:
                logger.info("[Baostock] 登录成功")
            else:
                logger.warning(f"[Baostock] 登录失败: {lg.error_msg}")
        except Exception as e:
            logger.warning(f"[Baostock] 登录异常: {e}")
    return _bs_logged_in


# ══════════════════════════════════════════════
# 1. 股票列表
# ══════════════════════════════════════════════

def get_stock_list() -> pd.DataFrame:
    """获取A股全市场股票列表

    Returns:
        DataFrame: ts_code, name, industry, list_date, market
    """
    # 1. AKShare (东财源)
    if ak is not None:
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                rename = {}
                for col in df.columns:
                    lc = col.lower()
                    if '代码' in col: rename[col] = 'ts_code'
                    elif '名称' in col: rename[col] = 'name'
                    elif '行业' in col: rename[col] = 'industry'
                    elif '上市' in col: rename[col] = 'list_date'
                df = df.rename(columns=rename)

                result = pd.DataFrame()
                if 'ts_code' in df.columns:
                    result['ts_code'] = df['ts_code'].astype(str).apply(_to_ts_code)
                if 'name' in df.columns:
                    result['name'] = df['name']
                if 'industry' in df.columns:
                    result['industry'] = df['industry'].fillna('')
                if 'list_date' in df.columns:
                    result['list_date'] = df['list_date'].astype(str).str.replace('-', '')
                result['market'] = 'A'
                result = result.dropna(subset=['ts_code'])
                result = result[result['ts_code'].str.len() >= 8]  # 000001.SZ
                logger.info(f"[StockList] AKShare 获取 {len(result)} 只")
                return result.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"[StockList] AKShare 失败: {e}")

    # 2. eFinance
    if efinance is not None:
        try:
            df = efinance.stock.get_all()
            if df is not None and not df.empty:
                result = pd.DataFrame()
                for col in df.columns:
                    lc = col.lower()
                    if 'code' in lc:
                        result['ts_code'] = df[col].astype(str).apply(_to_ts_code)
                    elif 'name' in lc or '名称' in col:
                        result['name'] = df[col]
                result['industry'] = ''
                result['list_date'] = ''
                result['market'] = 'A'
                result = result.dropna(subset=['ts_code'])
                logger.info(f"[StockList] eFinance 获取 {len(result)} 只")
                return result.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"[StockList] eFinance 失败: {e}")

    # 3. Baostock
    if _ensure_baostock():
        try:
            rs = bs.query_all_stock(day=datetime.now().strftime('%Y-%m-%d'))
            rows = []
            while (rs.error_code == '0') and rs.next():
                row = rs.get_row_data()
                code = row[0]
                if code.startswith('sh.'):
                    rows.append({'ts_code': f"{code[3:]}.SH", 'name': row[2], 'market': 'A'})
                elif code.startswith('sz.'):
                    rows.append({'ts_code': f"{code[3:]}.SZ", 'name': row[2], 'market': 'A'})
            result = pd.DataFrame(rows)
            result['industry'] = ''
            result['list_date'] = ''
            logger.info(f"[StockList] Baostock 获取 {len(result)} 只")
            return result.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"[StockList] Baostock 失败: {e}")

    logger.error("[StockList] 所有数据源均失败")
    return pd.DataFrame()


# ══════════════════════════════════════════════
# 2. 日线行情（OHLCV）
# ══════════════════════════════════════════════

def get_daily_history(ts_code: str, start_date: str, end_date: str,
                      adjust: str = 'qfq') -> pd.DataFrame:
    """获取单只股票日线历史

    Args:
        ts_code: 股票代码 000001.SZ
        start_date: 起始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
        adjust: qfq(前复权) / hfq(后复权) / ''(不复权)

    Returns:
        DataFrame: trade_date, open, high, low, close, vol, amount
    """
    code = ts_code.replace('.SH', '').replace('.SZ', '')

    # 1. AKShare
    if ak is not None:
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period='daily',
                start_date=_fmt_ymd(start_date),
                end_date=_fmt_ymd(end_date),
                adjust=adjust if adjust else ''
            )
            if df is not None and not df.empty:
                rename = {}
                for col in df.columns:
                    lc = col.lower()
                    if '日期' in col: rename[col] = 'trade_date'
                    elif '开盘' in col: rename[col] = 'open'
                    elif '最高' in col: rename[col] = 'high'
                    elif '最低' in col: rename[col] = 'low'
                    elif '收盘' in col: rename[col] = 'close'
                    elif '成交量' in col: rename[col] = 'vol'
                    elif '成交额' in col: rename[col] = 'amount'
                    elif '换手' in col: rename[col] = 'turnover'
                df = df.rename(columns=rename)
                if 'trade_date' in df.columns:
                    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
                df['ts_code'] = ts_code
                for col in ['open', 'high', 'low', 'close', 'vol', 'amount']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                return df[['trade_date', 'ts_code', 'open', 'high', 'low', 'close', 'vol', 'amount']].copy()
        except Exception as e:
            logger.debug(f"[DailyHist] AKShare {ts_code} 失败: {e}")

    # 2. eFinance
    if efinance is not None:
        try:
            df = efinance.stock.get_quote_history(code, klt=101)
            if df is not None and not df.empty:
                df = df.rename(columns={
                    '日期': 'trade_date', '开盘': 'open', '最高': 'high',
                    '最低': 'low', '收盘': 'close', '成交量': 'vol', '成交额': 'amount'
                })
                df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
                df['ts_code'] = ts_code
                return df[['trade_date', 'ts_code', 'open', 'high', 'low', 'close', 'vol', 'amount']].copy()
        except Exception as e:
            logger.debug(f"[DailyHist] eFinance {ts_code} 失败: {e}")

    # 3. Baostock
    if _ensure_baostock():
        try:
            bs_code = f"sh.{code}" if ts_code.endswith('.SH') else f"sz.{code}"
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=_fmt_ymd(start_date),
                end_date=_fmt_ymd(end_date),
                frequency="d",
                adjustflag="2" if adjust == 'qfq' else "3" if adjust == 'hfq' else "1"
            )
            rows = []
            while (rs.error_code == '0') and rs.next():
                row = rs.get_row_data()
                rows.append({
                    'trade_date': row[0],
                    'ts_code': ts_code,
                    'open': float(row[1]) if row[1] else np.nan,
                    'high': float(row[2]) if row[2] else np.nan,
                    'low': float(row[3]) if row[3] else np.nan,
                    'close': float(row[4]) if row[4] else np.nan,
                    'vol': float(row[5]) if row[5] else np.nan,
                    'amount': float(row[6]) if row[6] else np.nan,
                })
            return pd.DataFrame(rows)
        except Exception as e:
            logger.debug(f"[DailyHist] Baostock {ts_code} 失败: {e}")

    return pd.DataFrame()


# ══════════════════════════════════════════════
# 3. 实时行情
# ══════════════════════════════════════════════

def get_realtime_quotes(ts_codes: list = None) -> pd.DataFrame:
    """获取实时行情

    Args:
        ts_codes: 股票代码列表，None=全市场

    Returns:
        DataFrame: ts_code, name, price, pct_chg, volume, amount
    """
    # AKShare (新浪源)
    if ak is not None:
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                rename = {}
                for col in df.columns:
                    lc = col.lower()
                    if '代码' in col: rename[col] = 'ts_code'
                    elif '名称' in col: rename[col] = 'name'
                    elif '最新价' in col: rename[col] = 'price'
                    elif '涨跌幅' in col: rename[col] = 'pct_chg'
                    elif '成交量' in col: rename[col] = 'volume'
                    elif '成交额' in col: rename[col] = 'amount'
                    elif '总市值' in col: rename[col] = 'total_mv'
                df = df.rename(columns=rename)
                if 'ts_code' in df.columns:
                    df['ts_code'] = df['ts_code'].astype(str).apply(_to_ts_code)
                for col in ['price', 'pct_chg', 'volume', 'amount', 'total_mv']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                if ts_codes:
                    df = df[df['ts_code'].isin(ts_codes)]
                return df.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"[Realtime] AKShare 失败: {e}")

    return pd.DataFrame()


# ══════════════════════════════════════════════
# 4. ETF 数据
# ══════════════════════════════════════════════

def get_etf_list() -> pd.DataFrame:
    """获取全量 ETF 列表

    Returns:
        DataFrame: code, name, price, pct_chg, amount, total_mv
    """
    if ak is None:
        return pd.DataFrame()
    # 使用sina源作为主源，em源作为备用
    df = _get_etf_list_sina()
    if df is None or df.empty:
        df = _get_etf_list_em()
    if df is not None and not df.empty:
        df = df[~df['name'].str.contains('杠杆|反向|货币|国债|利率', na=False)]
        return df.reset_index(drop=True)
    return pd.DataFrame()


def _get_etf_list_sina() -> Optional[pd.DataFrame]:
    """使用sina接口获取ETF列表"""
    try:
        df = ak.fund_etf_category_sina(symbol="ETF基金")
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                if '代码' in col:
                    rename[col] = 'code'
                elif '名称' in col:
                    rename[col] = 'name'
                elif '最新价' in col:
                    rename[col] = 'price'
                elif '涨跌幅' in col:
                    rename[col] = 'pct_chg'
                elif '成交额' in col:
                    rename[col] = 'amount'
                elif '总市值' in col:
                    rename[col] = 'total_mv'
            df = df.rename(columns=rename)
            for col in ['price', 'pct_chg', 'amount', 'total_mv']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            return df
    except Exception as e:
        logger.warning(f"[ETFList Sina] 获取失败: {e}")
    return None


def _get_etf_list_em() -> Optional[pd.DataFrame]:
    """使用em接口获取ETF列表（备用）"""
    try:
        df = ak.fund_etf_spot_em()
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                if '代码' in col:
                    rename[col] = 'code'
                elif '名称' in col:
                    rename[col] = 'name'
                elif '最新价' in col:
                    rename[col] = 'price'
                elif '涨跌幅' in col:
                    rename[col] = 'pct_chg'
                elif '成交额' in col:
                    rename[col] = 'amount'
                elif '总市值' in col:
                    rename[col] = 'total_mv'
            df = df.rename(columns=rename)
            for col in ['price', 'pct_chg', 'amount', 'total_mv']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            return df
    except Exception as e:
        logger.warning(f"[ETFList EM] 获取失败: {e}")
    return None


def get_etf_history(code: str, days: int = 60) -> pd.DataFrame:
    """获取单只ETF历史K线

    Args:
        code: ETF代码 (6位纯数字，如 510300)
        days: 回溯天数

    Returns:
        DataFrame: date, close, volume
    """
    if ak is None:
        return pd.DataFrame()
    try:
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=days + 30)).strftime('%Y%m%d')
        df = ak.fund_etf_hist_em(
            symbol=code,
            period='daily',
            start_date=start,
            end_date=end,
            adjust='qfq'
        )
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                lc = col.lower()
                if '日期' in col: rename[col] = 'date'
                elif '收盘' in col: rename[col] = 'close'
                elif '成交量' in col: rename[col] = 'volume'
            df = df.rename(columns=rename)
            for col in ['close', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            return df.dropna(subset=['close']).tail(days).reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[ETFHist] {code} 失败: {e}")
    return pd.DataFrame()


# ══════════════════════════════════════════════
# 5. 可转债数据
# ══════════════════════════════════════════════

def get_convertible_bonds() -> pd.DataFrame:
    """获取可转债实时数据（集思录源，AKShare免费）

    Returns:
        DataFrame: cb_code, cb_name, stock_code, stock_name,
                   price, ytm, conversion_premium, remaining_size,
                   maturity_years, stock_mom_20
    """
    if ak is None:
        return pd.DataFrame()
    try:
        # 集思录可转债实时数据
        df = ak.bond_cb_jsl()
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                lc = col.lower()
                if '代码' in col and '正股' not in col: rename[col] = 'cb_code'
                elif '名称' in col and '正股' not in col: rename[col] = 'cb_name'
                elif '正股代码' in col: rename[col] = 'stock_code'
                elif '正股名称' in col: rename[col] = 'stock_name'
                elif '现价' in col or '转债价格' in col: rename[col] = 'price'
                elif '到期收益率' in col or 'ytm' in lc: rename[col] = 'ytm'
                elif '转股溢价率' in col: rename[col] = 'conversion_premium'
                elif '剩余规模' in col or '余额' in col: rename[col] = 'remaining_size'
                elif '剩余年限' in col or '到期时间' in col: rename[col] = 'maturity_years'
            df = df.rename(columns=rename)

            # 清理数值
            for col in ['price', 'ytm', 'conversion_premium', 'remaining_size', 'maturity_years']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col].astype(str).str.replace('%', ''), errors='coerce')

            # 正股20日动量（需要额外查询）
            if 'stock_code' in df.columns:
                df['stock_mom_20'] = _calc_stock_momentum(df['stock_code'].dropna().unique())

            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[ConvertibleBonds] 获取失败: {e}")

    # 降级：bond_zh_cov
    try:
        df = ak.bond_zh_cov()
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                lc = col.lower()
                if '债券代码' in col: rename[col] = 'cb_code'
                elif '债券名称' in col: rename[col] = 'cb_name'
                elif '正股代码' in col: rename[col] = 'stock_code'
                elif '正股名称' in col: rename[col] = 'stock_name'
            df = df.rename(columns=rename)
            df['ytm'] = np.nan
            df['conversion_premium'] = np.nan
            df['remaining_size'] = np.nan
            df['maturity_years'] = np.nan
            df['price'] = np.nan
            df['stock_mom_20'] = np.nan
            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[ConvertibleBonds] bond_zh_cov 也失败: {e}")

    return pd.DataFrame()


def _calc_stock_momentum(codes: list) -> dict:
    """批量计算正股20日动量"""
    result = {}
    for code in codes[:50]:  # 限制数量防超时
        try:
            bare = str(code).replace('.SH', '').replace('.SZ', '')
            hist = ak.stock_zh_a_hist(symbol=bare, period='daily',
                                      start_date=(datetime.now() - timedelta(days=30)).strftime('%Y%m%d'),
                                      end_date=datetime.now().strftime('%Y%m%d'))
            if hist is not None and len(hist) >= 21:
                close = pd.to_numeric(hist['收盘'], errors='coerce').dropna()
                if len(close) >= 21:
                    mom = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]
                    result[code] = mom
        except Exception:
            pass
        time.sleep(0.1)
    return result


# ══════════════════════════════════════════════
# 6. 指数成分股
# ══════════════════════════════════════════════

def get_index_constituents(index_code: str) -> pd.DataFrame:
    """获取指数成分股

    Args:
        index_code: 指数代码 000905(中证500) / 000300(沪深300)

    Returns:
        DataFrame: ts_code, name, weight
    """
    if ak is None:
        return pd.DataFrame()
    try:
        # 去掉 .SH/.SZ 后缀
        bare = index_code.replace('.SH', '').replace('.SZ', '')
        df = ak.index_stock_cons(symbol=bare)
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                lc = col.lower()
                if '成分' in col or '代码' in col: rename[col] = 'ts_code'
                elif '名称' in col: rename[col] = 'name'
                elif '权重' in col: rename[col] = 'weight'
            df = df.rename(columns=rename)
            if 'ts_code' in df.columns:
                df['ts_code'] = df['ts_code'].astype(str).apply(_to_ts_code)
            if 'weight' in df.columns:
                df['weight'] = pd.to_numeric(df['weight'], errors='coerce')
            else:
                df['weight'] = 1.0 / len(df)
            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[IndexCons] {index_code} 失败: {e}")
    return pd.DataFrame()


# ══════════════════════════════════════════════
# 7. 板块/概念数据
# ══════════════════════════════════════════════

def get_concept_list() -> pd.DataFrame:
    """获取概念板块列表

    Returns:
        DataFrame: concept_code, concept_name, pct_chg
    """
    if ak is None:
        return pd.DataFrame()
    try:
        df = ak.stock_board_concept_name_em()
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                lc = col.lower()
                if '板块代码' in col or '代码' in col: rename[col] = 'concept_code'
                elif '板块名称' in col or '名称' in col: rename[col] = 'concept_name'
                elif '涨跌幅' in col: rename[col] = 'pct_chg'
            df = df.rename(columns=rename)
            if 'pct_chg' in df.columns:
                df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[ConceptList] 失败: {e}")
    return pd.DataFrame()


def get_concept_stocks(concept_code: str) -> pd.DataFrame:
    """获取概念板块成分股

    Args:
        concept_code: 概念代码 (如 BK0655)

    Returns:
        DataFrame: ts_code, name, price, pct_chg
    """
    if ak is None:
        return pd.DataFrame()
    try:
        df = ak.stock_board_concept_cons_em(symbol=concept_code)
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                lc = col.lower()
                if '代码' in col: rename[col] = 'ts_code'
                elif '名称' in col: rename[col] = 'name'
                elif '最新价' in col: rename[col] = 'price'
                elif '涨跌幅' in col: rename[col] = 'pct_chg'
            df = df.rename(columns=rename)
            if 'ts_code' in df.columns:
                df['ts_code'] = df['ts_code'].astype(str).apply(_to_ts_code)
            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[ConceptStocks] {concept_code} 失败: {e}")
    return pd.DataFrame()


# ══════════════════════════════════════════════
# 8. 北向资金
# ══════════════════════════════════════════════

def get_northbound_flow(days: int = 30) -> pd.DataFrame:
    """获取北向资金流向（沪股通+深股通合计）

    Returns:
        DataFrame: trade_date, north_net_inflow, north_acc_inflow
    """
    from datetime import datetime, timedelta
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days + 60)).strftime('%Y%m%d')
    try:
        import tushare as ts
        from src.utils.config_loader import Config
        token = Config.tushare_token
        if token:
            pro = ts.pro_api(token)
            df = pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df = df[['trade_date', 'north_money']].rename(columns={'north_money': 'north_net_inflow'})
                df['north_net_inflow'] = pd.to_numeric(df['north_net_inflow'], errors='coerce') / 100.0
                df = df.sort_values('trade_date').reset_index(drop=True)
                df['north_acc_inflow'] = df['north_net_inflow'].cumsum()
                return df[['trade_date', 'north_net_inflow', 'north_acc_inflow']].tail(days).reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[Northbound-Tushare] 失败: {e}")
    if ak is None:
        return pd.DataFrame()
    try:
        df_sh = ak.stock_hsgt_hist_em(symbol='沪股通')
        df_sz = ak.stock_hsgt_hist_em(symbol='深股通')
        df_sh = df_sh[['日期', '当日成交净买额']].rename(columns={'当日成交净买额': 'sh_net'})
        df_sz = df_sz[['日期', '当日成交净买额']].rename(columns={'当日成交净买额': 'sz_net'})
        df = pd.merge(df_sh, df_sz, on='日期', how='outer').sort_values('日期').reset_index(drop=True)
        df['north_net_inflow'] = df['sh_net'].fillna(0) + df['sz_net'].fillna(0)
        df = df.dropna(subset=['north_net_inflow'])
        if df.empty:
            return pd.DataFrame(columns=['trade_date', 'north_net_inflow', 'north_acc_inflow'])
        df['north_acc_inflow'] = df['north_net_inflow'].cumsum()
        df = df.rename(columns={'日期': 'trade_date'})
        return df[['trade_date', 'north_net_inflow', 'north_acc_inflow']].tail(days).reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[Northbound-AKShare] 失败: {e}")
    return pd.DataFrame()


# ══════════════════════════════════════════════
# 9. 龙虎榜
# ══════════════════════════════════════════════

def get_lhb_data(start_date: str, end_date: str) -> pd.DataFrame:
    """获取龙虎榜数据

    Returns:
        DataFrame: trade_date, ts_code, name, lhb_net_buy
    """
    if ak is None:
        return pd.DataFrame()
    try:
        df = ak.stock_lhb_detail_em(
            start_date=_fmt_ymd(start_date),
            end_date=_fmt_ymd(end_date)
        )
        if df is not None and not df.empty:
            rename = {}
            for col in df.columns:
                lc = col.lower()
                if '代码' in col: rename[col] = 'ts_code'
                elif '名称' in col: rename[col] = 'name'
                elif '日期' in col: rename[col] = 'trade_date'
                elif '净买额' in col: rename[col] = 'lhb_net_buy'
            df = df.rename(columns=rename)
            if 'ts_code' in df.columns:
                df['ts_code'] = df['ts_code'].astype(str).apply(_to_ts_code)
            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[LHB] 失败: {e}")
    return pd.DataFrame()


# ══════════════════════════════════════════════
# 10. 宏观经济
# ══════════════════════════════════════════════

def get_macro_pmi() -> pd.DataFrame:
    """获取中国PMI数据

    Returns:
        DataFrame: month, pmi_manufacturing, pmi_non_manufacturing
    """
    if ak is None:
        return pd.DataFrame()
    try:
        df = ak.macro_china_pmi_yearly()
        if df is not None and not df.empty:
            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[MacroPMI] 失败: {e}")
    return pd.DataFrame()


def get_macro_cpi() -> pd.DataFrame:
    """获取中国CPI数据"""
    if ak is None:
        return pd.DataFrame()
    try:
        df = ak.macro_china_cpi_yearly()
        if df is not None and not df.empty:
            return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[MacroCPI] 失败: {e}")
    return pd.DataFrame()


# ══════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════

def _to_ts_code(code: str) -> str:
    """将纯数字代码转为 Tushare 格式 (000001.SZ)"""
    code = str(code).strip()
    if '.' in code:
        return code.upper()
    if len(code) == 6 and code.isdigit():
        if code.startswith('6'):
            return f"{code}.SH"
        else:
            return f"{code}.SZ"
    return code


def _fmt_ymd(date_str: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    s = str(date_str).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _fmt_ymd_reverse(date_str: str) -> str:
    """YYYY-MM-DD → YYYYMMDD"""
    return str(date_str).replace('-', '')
