"""
选股绩效跟踪器
每日收盘后运行，跟踪入选股票的N日收益率
"""
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Any

from loguru import logger
from src.utils.db_utils import DBUtils


class PerformanceTracker:
    """
    跟踪选股结果的实际表现

    每日收盘后调用 update_picks_performance()
    会为已入库但尚未有收益数据的 picks 计算N日收益率
    """

    def __init__(self):
        self.windows = [1, 5, 10, 20]

    def get_latest_picks_without_returns(self) -> pd.DataFrame:
        """获取已入库但尚未计算收益的 picks"""
        df = DBUtils.query_df("""
            SELECT trade_date, ts_code, name, entry_price,
                   ai_score, event_score, fundamental_score,
                   sector_momentum_score, track
            FROM pick_performance
            WHERE ret_1d IS NULL
            ORDER BY trade_date DESC
        """)
        return df

    @staticmethod
    def _fmt_date(d: str) -> str:
        """统一格式为 YYYY-MM-DD（stock_daily 的格式）"""
        if not d:
            return d
        d = d.replace('-', '')
        if len(d) == 8:
            return f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        return d

    def get_price_on_date(self, ts_code: str, trade_date: str) -> float:
        """获取指定日期收盘价"""
        td = self._fmt_date(trade_date)
        df = DBUtils.query_df("""
            SELECT close FROM stock_daily
            WHERE ts_code = ? AND trade_date = ?
        """, (ts_code, td))
        if not df.empty:
            return float(df.iloc[0]['close'])
        return None

    def get_latest_price(self, ts_code: str) -> float:
        """获取该股票最新收盘价"""
        df = DBUtils.query_df("""
            SELECT close FROM stock_daily
            WHERE ts_code = ?
            ORDER BY trade_date DESC LIMIT 1
        """, (ts_code,))
        if not df.empty:
            return float(df.iloc[0]['close'])
        return None

    def get_nth_trade_date(self, ts_code: str, start_date: str, n: int) -> str:
        """获取 start_date 之后第 n 个交易日"""
        sd = self._fmt_date(start_date)
        df = DBUtils.query_df("""
            SELECT trade_date FROM stock_daily
            WHERE ts_code = ? AND trade_date > ?
            ORDER BY trade_date
            LIMIT ?
        """, (ts_code, sd, n))
        if len(df) >= n:
            return str(df.iloc[n - 1]['trade_date'])
        return None

    def update_picks_performance(self) -> Dict[str, Any]:
        """
        每日收盘后调用，更新所有待跟踪的 picks 收益率
        """
        df = self.get_latest_picks_without_returns()
        if df.empty:
            logger.info("[PerformanceTracker] 无待更新的 picks")
            return {"updated": 0}

        logger.info(f"[PerformanceTracker] 待更新 {len(df)} 条 picks")
        updated = 0
        errors = 0

        for _, row in df.iterrows():
            try:
                ts_code = str(row['ts_code'])
                entry_date = str(row['trade_date'])
                entry_price = float(row['entry_price']) if row['entry_price'] else None

                if not entry_price:
                    continue

                # 获取最新价格（优先今日，没有则用最近交易日）
                today = datetime.now().strftime('%Y%m%d')
                current_price = self.get_price_on_date(ts_code, today)
                if not current_price:
                    current_price = self.get_latest_price(ts_code)

                updates = {}
                holding_days = 0

                if current_price and entry_price > 0:
                    ret_1d = (current_price - entry_price) / entry_price
                    updates['ret_1d'] = ret_1d

                    # 计算过去 N 日收益率
                    for n in [5, 10, 20]:
                        target_date = self.get_nth_trade_date(ts_code, entry_date, n)
                        if target_date:
                            price_nd = self.get_price_on_date(ts_code, target_date)
                            if price_nd and entry_price > 0:
                                ret = (price_nd - entry_price) / entry_price
                                updates[f'ret_{n}d'] = ret
                                if holding_days == 0:
                                    holding_days = n

                # 计算最大/最小收益（用 ret_1d 暂时充当，如果今日有数据）
                if 'ret_1d' in updates:
                    updates['ret_max'] = updates['ret_1d']
                    updates['ret_min'] = updates['ret_1d']
                    updates['holding_days'] = holding_days if holding_days > 0 else 1

                if updates:
                    set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
                    params = list(updates.values()) + [
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        ts_code, entry_date
                    ]
                    DBUtils.execute(
                        f"UPDATE pick_performance SET {set_clause}, updated_at = ? WHERE ts_code = ? AND trade_date = ?",
                        params
                    )
                    updated += 1

            except Exception as e:
                logger.debug(f"[PerformanceTracker] 更新失败 {row.get('ts_code')}: {e}")
                errors += 1

        logger.info(f"[PerformanceTracker] 更新完成: {updated} 成功, {errors} 失败")
        return {"updated": updated, "errors": errors}

    def record_picks(self, picks: List[Dict], trade_date: str):
        """
        选股完成后记录入选股票

        Args:
            picks: [{ts_code, name, close, final_score, ai_score, ...}, ...]
            trade_date: 选股日期
        """
        if not picks:
            return

        trade_date_fmt = trade_date.replace('-', '')
        saved = 0

        for p in picks:
            ts_code = str(p.get('ts_code', ''))
            if not ts_code:
                continue

            try:
                DBUtils.execute("""
                    INSERT INTO pick_performance
                    (trade_date, ts_code, name, entry_price, entry_score,
                     ai_score, event_score, fundamental_score, sector_momentum_score, track)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        entry_price = VALUES(entry_price),
                        entry_score = VALUES(entry_score),
                        ai_score = VALUES(ai_score),
                        event_score = VALUES(event_score),
                        fundamental_score = VALUES(fundamental_score),
                        sector_momentum_score = VALUES(sector_momentum_score),
                        track = VALUES(track)
                """, (
                    trade_date_fmt,
                    ts_code,
                    str(p.get('name', '')),
                    float(p.get('close') or 0),
                    float(p.get('final_score') or 0),
                    float(p.get('ai_score') or 0),
                    float(p.get('event_score') or 0),
                    float(p.get('fundamental_score') or p.get('fund_score') or 0),
                    float(p.get('sector_momentum_score') or 0),
                    str(p.get('track', '')),
                ))
                saved += 1
            except Exception as e:
                logger.debug(f"[record_picks] skip {ts_code}: {e}")

        logger.info(f"[PerformanceTracker] 记录 {saved} 只选股到 pick_performance")

    def get_performance_summary(self, days: int = 30) -> Dict[str, Any]:
        """获取近期绩效摘要"""
        start = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

        df = DBUtils.query_df(f"""
            SELECT
                COUNT(*) as total_picks,
                SUM(CASE WHEN ret_5d > 0 THEN 1 ELSE 0 END) as win_5d,
                SUM(CASE WHEN ret_5d IS NOT NULL THEN 1 ELSE 0 END) as counted_5d,
                AVG(ret_5d) as avg_ret_5d,
                MAX(ret_5d) as max_ret_5d,
                MIN(ret_5d) as min_ret_5d,
                AVG(ret_20d) as avg_ret_20d,
                SUM(CASE WHEN ret_20d > 0 THEN 1 ELSE 0 END) as win_20d
            FROM pick_performance
            WHERE trade_date >= '{start}'
        """)

        if df.empty:
            return {}

        r = df.iloc[0]
        counted_5d = int(r['counted_5d']) if r['counted_5d'] else 0

        return {
            "period_days": days,
            "total_picks": int(r['total_picks']) if r['total_picks'] else 0,
            "win_rate_5d": round(int(r['win_5d'] or 0) / counted_5d, 4) if counted_5d > 0 else None,
            "avg_ret_5d": round(float(r['avg_ret_5d'] or 0), 4),
            "max_ret_5d": round(float(r['max_ret_5d'] or 0), 4),
            "min_ret_5d": round(float(r['min_ret_5d'] or 0), 4),
            "avg_ret_20d": round(float(r['avg_ret_20d'] or 0), 4),
            "win_rate_20d": None,  # 需要单独计算
        }
