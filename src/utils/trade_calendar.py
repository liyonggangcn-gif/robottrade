"""
交易日历：判断指定日期是否为 A 股交易日。

优先从数据库 trade_calendar 表读取，若无则使用简单规则（周末休市 + 常见节假日）。
"""

import os
from datetime import datetime, date, timedelta

# 添加项目根目录
_src_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(os.path.dirname(_src_dir))


def _load_from_db(d: date) -> bool | None:
    """从 trade_calendar 表查询，存在且 is_open=1 则返回 True"""
    try:
        from src.utils.db_utils import DBUtils
        dt_str = d.strftime("%Y-%m-%d")
        
        # 先检查表是否存在且有数据
        try:
            table_check = DBUtils.query_df("SELECT COUNT(*) as cnt FROM trade_calendar")
            if table_check.empty or table_check.iloc[0]['cnt'] == 0:
                return None  # 表为空，使用简单规则
        except:
            return None  # 表不存在，使用简单规则
        
        # 查询具体日期
        df = DBUtils.query_df(
            "SELECT is_open FROM trade_calendar WHERE cal_date = ? LIMIT 1",
            params=[dt_str]
        )
        if df.empty:
            return None  # 查询不到该日期，使用简单规则
        return df.iloc[0]['is_open'] == 1  # 明确返回 True/False
    except Exception:
        return None  # 异常时使用简单规则


def _is_weekend(d: date) -> bool:
    """周六、周日休市"""
    return d.weekday() >= 5


# 常见 A 股节假日（春节、国庆等，格式 YYYYMMDD，可逐年补充）
_COMMON_HOLIDAYS = {
    "0101", "0501", "1001", "1002", "1003",  # 元旦、劳动节、国庆
}
# 春节、清明、端午、中秋等需按年维护，此处用简单规则：春节约 1 月底-2 月初
def _is_common_holiday(d: date) -> bool:
    md = d.strftime("%m%d")
    if md in _COMMON_HOLIDAYS:
        return True
    # 春节：约农历腊月廿八至正月初六
    if d.month == 1 and d.day >= 28:
        return True
    if d.month == 2 and d.day <= 10:
        return True
    return False


def is_trade_day(d: date | None = None) -> bool:
    """
    判断指定日期是否为 A 股交易日。

    Args:
        d: 日期，默认今天

    Returns:
        True=交易日, False=休市
    """
    if d is None:
        d = date.today()
    if isinstance(d, datetime):
        d = d.date()

    # 1. 优先查数据库
    from_db = _load_from_db(d)
    if from_db is not None:
        return from_db

    # 2. 简单规则：周末休市
    if _is_weekend(d):
        return False

    # 3. 常见节假日
    if _is_common_holiday(d):
        return False

    return True


def get_next_trade_day(d: date | None = None) -> date:
    """获取下一个交易日（含当天若为交易日）"""
    if d is None:
        d = date.today()
    if isinstance(d, datetime):
        d = d.date()
    for _ in range(30):
        if is_trade_day(d):
            return d
        d = d + timedelta(days=1)
    return d


def get_latest_trade_date_from_db() -> str | None:
    """从 stock_daily 表获取最新交易日"""
    try:
        from src.utils.db_utils import DBUtils
        df = DBUtils.query_df("SELECT MAX(trade_date) as d FROM stock_daily")
        if df.empty or df.iloc[0]['d'] is None:
            return None
        return str(df.iloc[0]['d']).strip()
    except Exception:
        return None
