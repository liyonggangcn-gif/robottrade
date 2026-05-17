"""
推荐标的追踪器：记录每只股票从被推荐（买入）到调出（卖出）的完整生命周期。

核心逻辑：
  - 每次选股后调用 update()，对比新老推荐列表
  - 新入选 → 记录买入日期、买入价（当日收盘价）
  - 仍在推荐 → 更新当前价格、计算浮动盈亏
  - 调出推荐 → 记录卖出日期、卖出价、最终盈亏
  - 历史全部保留，供日报展示和统计

数据表 recommendation_track：
  ts_code, name, buy_date, buy_price, sell_date, sell_price,
  profit_pct, holding_days, status(holding/sold), last_price, last_update
"""

import pandas as pd
from datetime import datetime
from src.utils.db_utils import DBUtils


class RecommendationTracker:
    """推荐标的追踪器"""

    def __init__(self):
        self._init_table()

    def _init_table(self):
        """创建追踪表（如不存在）"""
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS recommendation_track (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_code TEXT NOT NULL,
            name TEXT,
            buy_date TEXT NOT NULL,
            buy_price REAL,
            sell_date TEXT,
            sell_price REAL,
            profit_pct REAL,
            holding_days INTEGER DEFAULT 0,
            status TEXT DEFAULT 'holding',
            last_price REAL,
            last_update TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        # 索引加速查询
        try:
            DBUtils.execute('CREATE INDEX IF NOT EXISTS idx_rec_track_status ON recommendation_track(status)')
            DBUtils.execute('CREATE INDEX IF NOT EXISTS idx_rec_track_code ON recommendation_track(ts_code, status)')
        except Exception:
            pass

    def get_holding_list(self) -> pd.DataFrame:
        """获取当前所有 status='holding' 的推荐持仓"""
        df = DBUtils.query_df('''
            SELECT id, ts_code, name, buy_date, buy_price, holding_days,
                   last_price, profit_pct, last_update
            FROM recommendation_track
            WHERE status = 'holding'
            ORDER BY buy_date ASC
        ''')
        return df

    def get_recently_sold(self, limit: int = 10) -> pd.DataFrame:
        """获取最近卖出的标的（供日报展示）"""
        df = DBUtils.query_df(f'''
            SELECT ts_code, name, buy_date, sell_date, buy_price, sell_price,
                   profit_pct, holding_days
            FROM recommendation_track
            WHERE status = 'sold'
            ORDER BY sell_date DESC, id DESC
            LIMIT {limit}
        ''')
        return df

    def update(self, current_picks_df: pd.DataFrame, today_str: str = None):
        """
        核心方法：用本次选股结果更新追踪表。

        Args:
            current_picks_df: 本次选股结果 DataFrame，需包含 ts_code, name 列，
                              可选 close/current_price 列作为当前价格
            today_str: 当前日期，默认今天（YYYY-MM-DD）

        Returns:
            dict: {
                'new_entries': int,      # 新入选数量
                'still_holding': int,    # 继续持有数量
                'newly_sold': int,       # 本次调出数量
                'newly_sold_list': list, # 调出详情 [{'ts_code','name','profit_pct','holding_days'}, ...]
            }
        """
        if today_str is None:
            today_str = datetime.now().strftime('%Y-%m-%d')

        result = {
            'new_entries': 0,
            'still_holding': 0,
            'newly_sold': 0,
            'newly_sold_list': [],
        }

        # 当前推荐代码集合
        if current_picks_df is None or current_picks_df.empty or 'ts_code' not in current_picks_df.columns:
            current_codes = set()
        else:
            current_codes = set(current_picks_df['ts_code'].astype(str).str.strip())

        # 获取当前在持仓的推荐
        holding_df = self.get_holding_list()
        holding_codes = set()
        if not holding_df.empty:
            holding_codes = set(holding_df['ts_code'].astype(str).str.strip())

        # ---- 1. 调出的标的 → 标记为 sold ----
        sold_codes = holding_codes - current_codes
        if sold_codes and not holding_df.empty:
            for _, row in holding_df[holding_df['ts_code'].isin(sold_codes)].iterrows():
                rec_id = row['id']
                buy_price = row.get('buy_price') or 0
                # 尝试获取卖出价（用最近一次收盘价）
                sell_price = self._get_latest_close(row['ts_code'], today_str) or row.get('last_price') or buy_price
                profit_pct = ((sell_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
                buy_date = row.get('buy_date', '')
                holding_days = self._calc_holding_days(buy_date, today_str)
                DBUtils.execute('''
                    UPDATE recommendation_track
                    SET status = 'sold',
                        sell_date = ?,
                        sell_price = ?,
                        profit_pct = ?,
                        holding_days = ?,
                        last_price = ?,
                        last_update = ?
                    WHERE id = ?
                ''', (today_str, round(sell_price, 3), round(profit_pct, 2), holding_days, round(sell_price, 3), today_str, rec_id))
                result['newly_sold'] += 1
                result['newly_sold_list'].append({
                    'ts_code': row['ts_code'],
                    'name': row.get('name', ''),
                    'buy_date': buy_date,
                    'sell_date': today_str,
                    'buy_price': buy_price,
                    'sell_price': sell_price,
                    'profit_pct': round(profit_pct, 2),
                    'holding_days': holding_days,
                })

        # ---- 2. 继续持有的标的 → 更新价格和盈亏 ----
        still_codes = holding_codes & current_codes
        if still_codes and not holding_df.empty:
            for _, row in holding_df[holding_df['ts_code'].isin(still_codes)].iterrows():
                rec_id = row['id']
                buy_price = row.get('buy_price') or 0
                last_price = self._get_latest_close(row['ts_code'], today_str) or row.get('last_price') or buy_price
                profit_pct = ((last_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
                buy_date = row.get('buy_date', '')
                holding_days = self._calc_holding_days(buy_date, today_str)
                DBUtils.execute('''
                    UPDATE recommendation_track
                    SET last_price = ?,
                        profit_pct = ?,
                        holding_days = ?,
                        last_update = ?
                    WHERE id = ?
                ''', (round(last_price, 3), round(profit_pct, 2), holding_days, today_str, rec_id))
                result['still_holding'] += 1

        # ---- 3. 新入选的标的 → 插入买入记录 ----
        new_codes = current_codes - holding_codes
        if new_codes and current_picks_df is not None and not current_picks_df.empty:
            picks = current_picks_df.copy()
            picks['ts_code'] = picks['ts_code'].astype(str).str.strip()
            for _, row in picks[picks['ts_code'].isin(new_codes)].iterrows():
                ts_code = row['ts_code']
                name = row.get('name', '')
                # 买入价：优先用 close 列，否则查数据库
                buy_price = row.get('close') or row.get('current_price') or self._get_latest_close(ts_code, today_str) or 0
                DBUtils.execute('''
                    INSERT INTO recommendation_track
                    (ts_code, name, buy_date, buy_price, status, last_price, last_update, holding_days, profit_pct)
                    VALUES (?, ?, ?, ?, 'holding', ?, ?, 0, 0.0)
                ''', (ts_code, name, today_str, round(float(buy_price), 3), round(float(buy_price), 3), today_str))
                result['new_entries'] += 1

        return result

    def _get_latest_close(self, ts_code: str, date_str: str) -> float:
        """从 stock_daily 获取最近收盘价"""
        try:
            df = DBUtils.query_df(f'''
                SELECT close FROM stock_daily
                WHERE ts_code = ?
                ORDER BY trade_date DESC
                LIMIT 1
            ''', params=(ts_code,))
            if not df.empty:
                return float(df.iloc[0]['close'])
        except Exception:
            pass
        return None

    def _calc_holding_days(self, buy_date_str: str, today_str: str) -> int:
        """计算持仓天数"""
        try:
            buy = datetime.strptime(buy_date_str[:10], '%Y-%m-%d')
            today = datetime.strptime(today_str[:10], '%Y-%m-%d')
            return max(0, (today - buy).days)
        except Exception:
            return 0

    def format_for_dingtalk(self, current_picks_df: pd.DataFrame = None) -> str:
        """
        格式化持仓追踪（手机端短行优化，每行≤20中文字符）。
        """
        holding = self.get_holding_list()
        sold = self.get_recently_sold(limit=5)

        if holding.empty and sold.empty:
            return ""

        out = "\n**持仓追踪**\n"

        # 当前持仓
        if not holding.empty:
            total_pct = 0.0
            count = 0
            for _, row in holding.iterrows():
                name = (row.get('name') or '')[:4]
                code = (row.get('ts_code') or '')[:6]
                buy_mm_dd = (row.get('buy_date') or '')[5:10]  # MM-DD
                buy_price = row.get('buy_price', 0)
                last_price = row.get('last_price', 0)
                pct = row.get('profit_pct', 0)
                days = row.get('holding_days', 0)

                if pct > 0:
                    icon = "🟢"
                elif pct < 0:
                    icon = "🔴"
                else:
                    icon = "⚪"

                out += f"{icon}{name} {code} {pct:+.1f}%\n"
                out += f"  {buy_mm_dd}买{buy_price:.1f}→{last_price:.1f} {days}天\n"
                total_pct += pct
                count += 1

            avg_pct = total_pct / count if count > 0 else 0
            out += f"共{count}只 均{avg_pct:+.1f}%\n"

        # 最近卖出（只展示最近3条）
        if not sold.empty:
            out += "\n**近期调出**\n"
            for _, row in sold.head(3).iterrows():
                name = (row.get('name') or '')[:4]
                pct = row.get('profit_pct', 0)
                days = row.get('holding_days', 0)
                icon = "🟢" if pct >= 0 else "🔴"
                out += f"{icon}{name} {pct:+.1f}% {days}天\n"

            # 统计胜率
            stats = self.get_stats()
            if stats.get('total_sold', 0) > 0:
                out += f"胜率{stats['win_rate']}% 均盈{stats['avg_profit']:+.1f}%\n"

        return out

    def get_stats(self) -> dict:
        """统计历史胜率等"""
        try:
            sold_df = DBUtils.query_df('''
                SELECT profit_pct, holding_days FROM recommendation_track WHERE status = 'sold'
            ''')
            holding_df = self.get_holding_list()
            total_sold = len(sold_df)
            win = len(sold_df[sold_df['profit_pct'] > 0]) if total_sold > 0 else 0
            return {
                'holding_count': len(holding_df),
                'total_sold': total_sold,
                'win_count': win,
                'win_rate': round(win / total_sold * 100, 1) if total_sold > 0 else 0,
                'avg_profit': round(sold_df['profit_pct'].mean(), 2) if total_sold > 0 else 0,
                'avg_holding_days': round(sold_df['holding_days'].mean(), 1) if total_sold > 0 else 0,
            }
        except Exception:
            return {'holding_count': 0, 'total_sold': 0, 'win_count': 0, 'win_rate': 0, 'avg_profit': 0, 'avg_holding_days': 0}
