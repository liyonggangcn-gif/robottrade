"""
每日热门板块及龙头股分析推送
基于涨幅榜识别热门板块，对龙头股进行LLM分析，通过钉钉推送
支持每30分钟检查，有变化才推送
数据存储到数据库供复盘
自动搜索板块相关新闻/研报/公告
使用ClickHouse加速查询
"""
import sys
import os
sys.path.insert(0, '.')

import pandas as pd
from datetime import datetime, timedelta


def get_ch_client():
    """获取ClickHouse客户端"""
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host='192.168.3.51',
        port=8123,
        username='default',
        password='clickhouse123'
    )


from src.utils.llm_client import LLMClient
from src.utils.notifier import send_alert
from src.utils.config_loader import Config


def ensure_table():
    """确保热门板块数据表存在 - MySQL"""
    try:
        from src.utils.db_utils import DBUtils
        
        # 创建表
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS sector_hot (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date VARCHAR(10) NOT NULL,
                check_time VARCHAR(8) NOT NULL,
                sector VARCHAR(100) NOT NULL,
                stock_count INT DEFAULT 0,
                avg_chg FLOAT DEFAULT 0,
                max_chg FLOAT DEFAULT 0,
                leader_code VARCHAR(20),
                leader_name VARCHAR(100),
                leader_chg FLOAT,
                related_etfs TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_date_sector (trade_date, sector)
            )
        """)
        
        # 尝试添加新列（如果表已存在）
        try:
            DBUtils.execute("ALTER TABLE sector_hot ADD COLUMN related_etfs TEXT")
        except:
            pass
            
        print("[OK] sector_hot table ready")
    except Exception as e:
        print(f"[WARN] table create: {e}")


def ensure_ch_table():
    """确保ClickHouse表存在"""
    try:
        client = get_ch_client()
        client.command("""
            CREATE TABLE IF NOT EXISTS sector_hot (
                trade_date String,
                check_time String,
                sector String,
                stock_count Int32,
                avg_chg Float32,
                max_chg Float32,
                leader_code String,
                leader_name String,
                leader_chg Float32,
                created_at DateTime DEFAULT now()
            )
            ENGINE = MergeTree()
            ORDER BY (trade_date, sector)
        """)
        print("[OK] sector_hot CH table ready")
    except Exception as e:
        print(f"[WARN] CH table create: {e}")


def get_prev_hot_sectors(trade_date):
    """获取上一次的热门板块记录"""
    # 默认返回空，依赖数据库已有记录
    try:
        from src.utils.db_utils import DBUtils
        df = DBUtils.query_df("""
            SELECT sector, leader_code, avg_chg
            FROM sector_hot
            WHERE trade_date = ?
            ORDER BY avg_chg DESC
            LIMIT 5
        """, params=[trade_date])
        return df.to_dict('records') if not df.empty else []
    except:
        return []


def save_sector_hot(trade_date, check_time, hot_sectors, sector_leaders, sector_etfs=None):
    """保存热门板块数据到数据库 - ClickHouse + MySQL"""
    if sector_etfs is None:
        sector_etfs = {}
    
    count = 0
    for sector, data in hot_sectors.items():
        leader = sector_leaders.get(sector, {})
        
        # 获取相关ETF
        etfs = sector_etfs.get(sector, [])
        etfs_text = ";".join([f"{e['name']}({e['code']})" for e in etfs]) if etfs else ""
        
        row = {
            'trade_date': trade_date,
            'check_time': check_time,
            'sector': sector,
            'stock_count': data.get('stock_count', 0),
            'avg_chg': data.get('avg_chg', 0),
            'max_chg': data.get('max_chg', 0),
            'leader_code': leader.get('ts_code', ''),
            'leader_name': leader.get('name', ''),
            'leader_chg': leader.get('pct_chg', 0),
            'related_etfs': etfs_text
        }

        # 尝试ClickHouse
        try:
            client = get_ch_client()
            client.command("""
                INSERT INTO sector_hot
                (trade_date, check_time, sector, stock_count, avg_chg, max_chg, leader_code, leader_name, leader_chg, related_etfs)
                VALUES
                ('{trade_date}', '{check_time}', '{sector}', {stock_count}, {avg_chg}, {max_chg}, '{leader_code}', '{leader_name}', {leader_chg}, '{related_etfs}')
            """.format(**row))
            count += 1
            continue
        except Exception as e:
            print(f"[CH save] {e}")

        # 回退MySQL
        try:
            from src.utils.db_utils import DBUtils
            DBUtils.execute("""
                INSERT INTO sector_hot
                    (trade_date, check_time, sector, stock_count, avg_chg, max_chg,
                     leader_code, leader_name, leader_chg, related_etfs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    stock_count = VALUES(stock_count),
                    avg_chg = VALUES(avg_chg),
                    max_chg = VALUES(max_chg),
                    leader_code = VALUES(leader_code),
                    leader_name = VALUES(leader_name),
                    leader_chg = VALUES(leader_chg),
                    related_etfs = VALUES(related_etfs),
                    check_time = VALUES(check_time)
            """, (
                trade_date, check_time, sector,
                data.get('stock_count', 0),
                data.get('avg_chg', 0),
                data.get('max_chg', 0),
                leader.get('ts_code', ''),
                leader.get('name', ''),
                leader.get('pct_chg', 0),
                etfs_text
            ))
            count += 1
        except Exception as e:
            print(f"[MySQL save] {e}")
    
    print(f"[OK] saved {count} sectors")
    return count


def get_top_gainers(trade_date, top_n=50):
    """获取今日涨幅榜 - ClickHouse加速版"""
    try:
        client = get_ch_client()

        # 标准化日期 (去掉-)
        td = trade_date.replace('-', '')

        # 获取前一天 - 尝试两种格式
        result = client.query(f"SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < '{td}'")
        rows = result.result_rows
        prev_date = rows[0][0] if rows and rows[0][0] else None
        
        if not prev_date:
            # 尝试带-格式
            result2 = client.query(f"SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < '{trade_date}'")
            rows2 = result2.result_rows
            prev_date = rows2[0][0] if rows2 and rows2[0][0] else None

        if not prev_date:
            return pd.DataFrame()

        # 简化SQL - 直接用子查询获取前一天收盘价
        # 尝试两种日期格式查询
        sql = f"""
        SELECT
            d.ts_code as ts_code,
            d.close as close,
            if(p.close > 0, (d.close / p.close - 1) * 100, 0) AS pct_chg,
            i.industry as industry
        FROM stock_daily d
        LEFT JOIN stock_info i ON d.ts_code = i.ts_code
        LEFT JOIN stock_daily p ON d.ts_code = p.ts_code AND p.trade_date = '{prev_date}'
        WHERE d.trade_date = '{td}' AND d.close > 0
        ORDER BY pct_chg DESC
        LIMIT {top_n}
        """
        result = client.query(sql)
        rows = result.result_rows

        if not rows or len(rows) == 0:
            # 尝试标准格式日期查询
            sql2 = f"""
            SELECT
                d.ts_code as ts_code,
                d.close as close,
                if(p.close > 0, (d.close / p.close - 1) * 100, 0) AS pct_chg,
                i.industry as industry
            FROM stock_daily d
            LEFT JOIN stock_info i ON d.ts_code = i.ts_code
            LEFT JOIN stock_daily p ON d.ts_code = p.ts_code AND p.trade_date = '{prev_date}'
            WHERE d.trade_date = '{trade_date}' AND d.close > 0
            ORDER BY pct_chg DESC
            LIMIT {top_n}
            """
            result2 = client.query(sql2)
            rows = result2.result_rows

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=['ts_code', 'close', 'pct_chg', 'industry'])
        df['name'] = df['ts_code']
        
        return df

    except Exception as e:
        print(f"[CH Error] {e}")
        return _get_top_gainers_mysql(trade_date, top_n)


def _get_top_gainers_mysql(trade_date, top_n=50):
    """获取今日涨幅榜 - MySQL回退"""
    from src.utils.db_utils import DBUtils

    prev_dates = DBUtils.query_df(
        "SELECT trade_date FROM stock_daily WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1",
        params=[trade_date]
    )
    if prev_dates.empty:
        return pd.DataFrame()
    prev_date = prev_dates.iloc[0]['trade_date']

    sql = f"""
    SELECT
        sd.ts_code,
        COALESCE(si.name, sd.ts_code) AS name,
        sd.close,
        (sd.close / p.close - 1) * 100 AS pct_chg,
        si.industry
    FROM stock_daily sd
    LEFT JOIN stock_info si ON sd.ts_code COLLATE utf8mb4_general_ci = si.ts_code COLLATE utf8mb4_general_ci
    INNER JOIN (
        SELECT ts_code COLLATE utf8mb4_general_ci as ts_code, close as close FROM stock_daily WHERE trade_date = ?
    ) p ON sd.ts_code COLLATE utf8mb4_general_ci = p.ts_code
    WHERE sd.trade_date = ? AND p.close > 0 AND sd.close > 0
    ORDER BY pct_chg DESC
    LIMIT ?
    """
    return DBUtils.query_df(sql, params=[prev_date, trade_date, top_n])


def get_hot_concepts_akshare():
    """使用akshare获取热门概念板块 - 回退到行业板块"""
    try:
        import akshare as ak
        
        # 尝试多个API
        apis_to_try = [
            ('stock_board_industry_name_em', {}),
        ]
        
        for api_name, kwargs in apis_to_try:
            try:
                func = getattr(ak, api_name, None)
                if func:
                    df = func(**kwargs)
                    if df is not None and not df.empty:
                        # 尝试找涨幅列
                        chg_col = None
                        for col in ['涨跌幅', '涨幅', '涨跌幅(%)', '行业涨跌幅']:
                            if col in df.columns:
                                chg_col = col
                                break
                        
                        name_col = None
                        for col in ['板块名称', '名称', '行业', '板块']:
                            if col in df.columns:
                                name_col = col
                                break
                        
                        if name_col and chg_col:
                            df = df.sort_values(chg_col, ascending=False).head(10)
                            
                            # 尝试找股票数列
                            count_col = None
                            for col in ['股票数', '成分股数量', '数量']:
                                if col in df.columns:
                                    count_col = col
                                    break
                            
                            result = {}
                            for _, row in df.iterrows():
                                name = row.get(name_col, '')
                                chg = row.get(chg_col, 0) or 0
                                stock_count = row.get(count_col, 0) if count_col else 0
                                if name and chg is not None:
                                    result[name] = {
                                        'avg_chg': float(chg),
                                        'fund_flow': 0,
                                        'stock_count': int(stock_count) if stock_count else 10
                                    }
                            
                            print(f"[AKShare] {api_name}: {len(result)} 个板块")
                            return result
            except Exception as e:
                print(f"[AKShare {api_name}] {str(e)[:50]}")
                continue
        
        return {}
    except Exception as e:
        print(f"[AKShare Error] {e}")
        return {}


def get_hot_sectors_tushare():
    """使用Tushare获取热门行业板块"""
    try:
        import tushare as ts
        from src.utils.config_loader import Config
        
        token = Config.get('tushare_token', '')
        if not token:
            return {}
        
        ts.set_token(token)
        pro = ts.pro_api()
        
        # 获取最新交易日
        cal = pro.trade_cal(exchange='SSE', start_date='20260401', end_date='20260420')
        trading_dates = cal[cal['is_open']==1]['cal_date'].tolist()
        if not trading_dates:
            return {}
        trade_date = trading_dates[-1]
        
        # 使用daily接口获取涨跌数据
        df = pro.daily(trade_date=trade_date, fields='ts_code,close,pre_close,pct_chg')
        if df is None or df.empty:
            return {}
        
        # 获取行业信息
        stock_info = pro.stock_basic(fields='ts_code,industry')
        
        # 合并
        df = df.merge(stock_info, on='ts_code', how='left')
        
        # 过滤有行业的
        df = df[df['industry'].notna()]
        
        # 按行业分组计算
        industry_stats = df.groupby('industry').agg({
            'pct_chg': ['mean', 'count', 'max']
        }).reset_index()
        
        industry_stats.columns = ['industry', 'avg_chg', 'count', 'max_chg']
        industry_stats = industry_stats[industry_stats['count'] >= 3]
        industry_stats = industry_stats.sort_values('avg_chg', ascending=False)
        
        result = {}
        for _, row in industry_stats.head(10).iterrows():
            if row['industry']:
                result[row['industry']] = {
                    'avg_chg': float(row['avg_chg']),
                    'stock_count': int(row['count']),
                    'max_chg': float(row['max_chg'])
                }
        
        print(f"[Tushare] 热门行业: {len(result)} 个")
        return result
        
    except Exception as e:
        print(f"[Tushare Error] {e}")
        return {}


def get_concept_stocks(concept_name, limit=10):
    """获取概念板块内的成分股"""
    try:
        import akshare as ak
        # 获取概念板块成分股
        df = ak.stock_board_concept_cons_em(symbol=concept_name)
        if df is not None and not df.empty:
            # 取涨幅前列
            if '涨跌幅' in df.columns:
                df = df.sort_values('涨跌幅', ascending=False)
            return df.head(limit)
    except Exception as e:
        print(f"[Concept stocks] {e}")
    
    # 回退到数据库
    try:
        from src.utils.db_utils import DBUtils
        sql = f"""
            SELECT sc.ts_code, si.name, sd.close, 
                   (sd.close / p.close - 1) * 100 AS pct_chg
            FROM stock_concepts sc
            JOIN stock_info si ON sc.ts_code = si.ts_code
            JOIN stock_daily sd ON sc.ts_code = sd.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = (
                SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < CURDATE()
            )) p ON sc.ts_code = p.ts_code
            WHERE sc.concept_name LIKE %s AND sd.trade_date = (
                SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < CURDATE()
            )
            ORDER BY pct_chg DESC LIMIT {limit}
        """
        df = DBUtils.query_df(sql, params=[f'%{concept_name}%'])
        return df
    except:
        return pd.DataFrame()


def identify_hot_sectors(gainers_df, min_stocks=3):
    """识别热门板块"""
    if gainers_df.empty or 'industry' not in gainers_df.columns:
        return {}

    sector_stats = gainers_df.groupby('industry').agg({
        'pct_chg': ['count', 'mean', 'max'],
        'ts_code': lambda x: list(x)
    }).reset_index()
    sector_stats.columns = ['industry', 'stock_count', 'avg_chg', 'max_chg', 'codes']
    sector_stats = sector_stats[sector_stats['stock_count'] >= min_stocks]
    sector_stats = sector_stats.sort_values('avg_chg', ascending=False)

    hot_sectors = {}
    for _, row in sector_stats.head(8).iterrows():
        hot_sectors[row['industry']] = {
            'stock_count': int(row['stock_count']),
            'avg_chg': float(row['avg_chg']),
            'max_chg': float(row['max_chg']),
            'codes': row['codes'][:5]
        }
    return hot_sectors


def analyze_leader_stock(stock_data, llm_client):
    """精简龙头股分析"""
    if not stock_data or not llm_client.is_available():
        return ""

    name = stock_data.get('name', stock_data.get('ts_code', ''))
    pct = stock_data.get('pct_chg', 0)
    code = stock_data.get('ts_code', '')

    prompt = f"简述{name}({code})上涨原因和看点，50字内"

    try:
        result = llm_client._call_llm(
            system_prompt="你是股市点评师",
            user_prompt=prompt,
            temperature=0.3,
            max_tokens=60
        )
        return result[:80] if result else ""
    except:
        return ""


def check_changes(prev_sectors, current_sectors):
    """检查热门板块是否有变化"""
    if not prev_sectors:
        return True, "首次记录"

    prev_set = set(s['sector'] for s in prev_sectors)
    curr_set = set(current_sectors.keys())

    # 新增板块
    added = curr_set - prev_set
    # 消失板块
    removed = prev_set - curr_set
    # 板块内排名变化
    changed = False

    for sector in prev_set & curr_set:
        prev_avg = next((s['avg_chg'] for s in prev_sectors if s['sector'] == sector), 0)
        curr_avg = current_sectors[sector]['avg_chg']
        if abs(curr_avg - prev_avg) > 3:  # 涨幅变化超过3%
            changed = True
            break

    if added or removed or changed:
        reason = []
        if added:
            reason.append(f"新增{len(added)}板块")
        if removed:
            reason.append(f"消失{len(removed)}板块")
        if changed:
            reason.append("排名变化")
        return True, "+".join(reason) if reason else "有变化"

    return False, "无变化"


def get_sector_news(sector_keywords, hours=72):
    """从news_cache获取板块相关新闻"""
    try:
        from datetime import timedelta
        from src.utils.db_utils import DBUtils
        since = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d')

        kws = [kw.replace("'", "") for kw in sector_keywords[:3]]
        cond = " OR ".join(f"title LIKE '%{k}%'" for k in kws)
        if not cond:
            return []

        df = DBUtils.query_df(f"""
            SELECT title, source, published_at
            FROM news_cache
            WHERE published_at >= '{since}' AND ({cond})
            ORDER BY published_at DESC
            LIMIT 5
        """)
        if df.empty:
            return []
        
        return [(row['title'], row['source'], str(row['published_at'])) for _, row in df.iterrows()]
    except Exception as e:
        print(f"[Sector news] {e}")
        return []


def get_related_etfs(sector_name):
    """获取与板块相关的ETF"""
    try:
        from src.utils.db_utils import DBUtils
        
        # 从etf_daily表获取
        etf_df = DBUtils.query_df('''
            SELECT code, name, pct_chg, amount
            FROM etf_daily
            WHERE trade_date = (SELECT MAX(trade_date) FROM etf_daily)
            AND pct_chg IS NOT NULL
            ORDER BY amount DESC
            LIMIT 100
        ''')
        
        if etf_df.empty:
            return []
        
        # 通过名称匹配找到相关的ETF
        sector_keywords = sector_name.lower().split()
        related_etfs = []
        
        for _, row in etf_df.iterrows():
            name = (row.get('name') or '').lower()
            code = row.get('code', '')
            
            # 检查ETF名称是否包含板块关键词
            matched = any(kw in name for kw in sector_keywords if len(kw) > 1)
            
            # 额外匹配一些常见的板块ETF
            if '半导体' in sector_name or '芯片' in sector_name:
                matched = matched or '芯片' in name or '半导体' in name
            elif '新能源' in sector_name or '光伏' in sector_name:
                matched = matched or '新能源' in name or '光伏' in name or '锂电' in name
            elif '医药' in sector_name:
                matched = matched or '医药' in name or '医疗' in name
            elif '军工' in sector_name:
                matched = matched or '军工' in name
            elif 'AI' in sector_name or '人工智能' in sector_name:
                matched = matched or '人工智能' in name or 'AI' in name or '科技' in name
            
            if matched:
                related_etfs.append({
                    'code': code,
                    'name': row.get('name', ''),
                    'chg': row.get('pct_chg', 0) or 0
                })
        
        # 返回涨幅前3的ETF
        related_etfs.sort(key=lambda x: x['chg'], reverse=True)
        return related_etfs[:3]
        
    except Exception as e:
        print(f"[ETF] {e}")
        return []

        sql = f"SELECT title, source, published_at FROM news_cache WHERE ({cond}) AND published_at >= '{since}' ORDER BY published_at LIMIT 5"
        df = DBUtils.query_df(sql)
        if df.empty:
            return []
        return [(str(r.title)[:60], str(r.source)[:15], str(r.published_at)[:16]) for _, r in df.iterrows()]
    except:
        return []


def get_sector_reports(sector_name, hours=72):
    """获取板块相关研报"""
    try:
        from datetime import timedelta
        since = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d')
        sector_clean = sector_name.replace("'", "")

        try:
            sql = f"""
                SELECT title, org_name, report_date
                FROM research_reports
                WHERE title LIKE '%{sector_clean}%'
                  AND report_date >= '{since}'
                ORDER BY report_date DESC
                LIMIT 5
            """
            df = DBUtils.query_df(sql)
            if df.empty:
                return []

            reports = []
            for _, row in df.iterrows():
                title = str(row.get('title', ''))[:60]
                org = str(row.get('org_name', ''))[:15]
                reports.append((title, org))
            return reports
        except:
            pass
    except Exception as e:
        return []
    return []


def get_sector_announcements(sector_name, hours=48):
    """获取板块相关公告"""
    try:
        from datetime import timedelta
        since = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d')
        sector_clean = sector_name.replace("'", "")

        try:
            sql = f"""
                SELECT title, pub_date
                FROM stock_announcements
                WHERE title LIKE '%{sector_clean}%'
                  AND pub_date >= '{since}'
                ORDER BY pub_date DESC
                LIMIT 5
            """
            df = DBUtils.query_df(sql)
            if df.empty:
                return []

            announcements = []
            for _, row in df.iterrows():
                title = str(row.get('title', ''))[:60]
                date = str(row.get('pub_date', ''))[-5:]
                announcements.append((title, date))
            return announcements
        except:
            pass
    except Exception as e:
        return []
    return []


def get_sector_stocks(sector_name, limit=10):
    """获取板块内股票列表及基本信息"""
    try:
        from src.utils.db_utils import DBUtils
        sql = f"""
            SELECT ts_code, name, close, total_mv, pe_ttm, industry
            FROM stock_daily sd
            INNER JOIN stock_info si ON sd.ts_code = si.ts_code
            WHERE si.industry LIKE '%{sector_name}%'
            ORDER BY sd.total_mv DESC
            LIMIT {limit}
        """
        df = DBUtils.query_df(sql)
        if df.empty:
            return []
        return df.to_dict('records')
    except Exception as e:
        return []


def analyze_sector_reason(sector_name, sector_stocks, llm_client):
    """分析板块上涨原因 - 结合新闻/概念/业绩"""
    if not llm_client.is_available():
        return ""
    
    # 获取板块相关新闻
    from src.utils.db_utils import DBUtils
    news = []
    try:
        cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        news_df = DBUtils.query_df("""
            SELECT title, source FROM news_cache
            WHERE published_at >= %s AND (title LIKE %s OR title LIKE %s)
            ORDER BY published_at DESC LIMIT 5
        """, params=[cutoff, f'%{sector_name[:2]}%', f'%{sector_name}%'])
        if not news_df.empty:
            news = [row['title'][:30] for _, row in news_df.iterrows()]
    except:
        pass
    
    # 获取板块内涨停/领涨股票
    top_stocks = sector_stocks[:3] if sector_stocks else []
    stock_names = "、".join([s.get('name', s.get('ts_code', ''))[:6] for s in top_stocks])
    
    # 构建分析提示
    news_text = "；".join(news[:3]) if news else "暂无新闻"
    prompt = f"分析{sector_name}板块今日大涨原因。已知：近期新闻「{news_text}」，领涨股「{stock_names}」。请总结1-2个核心驱动因素，30字内"
    
    try:
        result = llm_client._call_llm(
            system_prompt="你是股市策略分析师，擅长解读板块上涨原因",
            user_prompt=prompt,
            temperature=0.3,
            max_tokens=40
        )
        return result[:50] if result else ""
    except:
        return ""


def recommend_sector_stocks(sector_name, sector_stocks, llm_client, limit=3):
    """推荐板块内值得关注股票 - 综合考虑涨幅/基本面/热度"""
    if not sector_stocks:
        return []
    
    # 简单排序：涨幅 * 0.4 + 热度(字数) * 0.3 + 随机 * 0.3
    recommended = []
    for stock in sector_stocks[:10]:
        score = 0
        # 涨幅得分
        chg = stock.get('pct_chg', 0) or 0


def recommend_sector_stocks_v2(sector_name, sector_stocks, llm_client, limit=3):
    """推荐板块内值得关注股票 - AKShare版"""
    if not sector_stocks:
        return []
    
    # 解析AKShare返回的数据格式
    recommended = []
    for stock in sector_stocks[:15]:
        score = 0
        
        # 涨幅得分 (40%)
        chg = stock.get('涨跌幅', 0) or 0
        if chg is None:
            chg = 0
        score += min(chg / 10, 1) * 0.4 if chg > 0 else 0
        
        # 主力净流入 (30%)
        flow = stock.get('主力净流入', 0) or 0
        if flow and float(flow) > 0:
            score += 0.3
        
        # 市值合理 - 尝试获取 (20%)
        # 跳过市值检查，直接给分
        
        # 换手率 (10%)
        turnover = stock.get('换手率', 0) or 0
        if turnover and float(turnover) > 5:
            score += 0.1
        
        stock['_score'] = score
        recommended.append(stock)
    
    # 排序取top
    recommended.sort(key=lambda x: x.get('_score', 0), reverse=True)
    return recommended[:limit]


def analyze_stock_recommend(stock, llm_client):
    """分析推荐股票的投资逻辑"""
    if not llm_client.is_available():
        return ""
    
    name = stock.get('name', stock.get('ts_code', ''))
    code = stock.get('ts_code', '')
    chg = stock.get('pct_chg', 0) or 0
    industry = stock.get('industry', '')
    
    prompt = f"简要分析{name}({code})今日{chg:+.1f}%的上涨逻辑，说明为何值得关注，40字内"
    
    try:
        result = llm_client._call_llm(
            system_prompt="你是股票分析师，擅长解读个股上涨逻辑",
            user_prompt=prompt,
            temperature=0.3,
            max_tokens=50
        )
        return result[:60] if result else ""
    except:
        return ""


def build_push_message(hot_sectors, sector_analysis, sector_news, sector_company_analysis, trade_date, check_time):
    """构建精简推送消息"""
    lines = [f"## 🔥 热门板块 ({trade_date[5:]} {check_time})", ""]

    # 板块排行
    sorted_sectors = sorted(hot_sectors.items(), key=lambda x: x[1]['avg_chg'], reverse=True)
    for sector, data in sorted_sectors[:5]:
        lines.append(f"**{sector}**: {data['avg_chg']:+.1f}% ({data['stock_count']}只)")

    # 新闻
    if sector_news:
        lines.append("")
        for sector, news_list in list(sector_news.items())[:1]:
            if news_list and news_list[0]:
                title, source, time = news_list[0]
                lines.append(f"📰 {title[:50]}")

    # 龙头股
    if sector_analysis and sector_analysis.get(list(sector_analysis.keys())[0], ""):
        lines.append("")
        for sector, analysis in sector_analysis.items():
            if analysis:
                lines.append(f"🏆 {analysis[:80]}")

    # 板块公司
    if sector_company_analysis and sector_company_analysis.get(list(sector_company_analysis.keys())[0], ""):
        lines.append("")
        for sector, analysis in sector_company_analysis.items():
            if analysis:
                lines.append(f"📊 {sector}: {analysis[:100]}")

    return "\n".join(lines)


def build_push_message_v2(hot_sectors, sector_analysis, sector_reason, 
                         sector_recommend, sector_news, trade_date, check_time, sector_etfs=None):
    """构建增强版推送消息 - 包含板块原因和推荐股票"""
    if sector_etfs is None:
        sector_etfs = {}
    
    lines = [f"## 🔥 热门板块分析 ({trade_date[5:]} {check_time})", ""]
    
    # 板块排行 + 原因 + 推荐 + ETF
    sorted_sectors = sorted(hot_sectors.items(), key=lambda x: x[1]['avg_chg'], reverse=True)
    
    for sector, data in sorted_sectors[:5]:
        # 基本信息
        stock_count = data.get('stock_count', 10)
        lines.append(f"### {sector} ({data['avg_chg']:+.1f}%, {stock_count}只)")
        
        # 相关ETF
        etfs = sector_etfs.get(sector, [])
        if etfs:
            lines.append("📊 相关ETF:")
            for etf in etfs:
                lines.append(f"  • {etf['name']}({etf['code']}) {etf['chg']:+.1f}%")
        
        # 上涨原因
        reason = sector_reason.get(sector, '')
        if reason:
            lines.append(f"📌 原因: {reason}")
        
        # 上涨原因
        reason = sector_reason.get(sector, '')
        if reason:
            lines.append(f"📌 原因: {reason}")
        
        # 推荐股票
        recs = sector_recommend.get(sector, [])
        if recs:
            lines.append("⭐ 关注:")
            for rec in recs:
                name = rec.get('name', '')
                code = rec.get('code', '')
                chg = rec.get('chg', 0)
                logic = rec.get('logic', '')[:30]
                if code:
                    lines.append(f"  • {name}({code}) {chg:+.1f}% - {logic}")
        
        # 龙头股分析
        analysis = sector_analysis.get(sector, '')
        if analysis:
            lines.append(f"🏆 {analysis}")
        
        lines.append("")
    
    # 相关新闻
    if sector_news:
        lines.append("### 📰 板块新闻")
        for sector, news_list in list(sector_news.items())[:2]:
            if news_list and news_list[0]:
                title, source, time = news_list[0]
                lines.append(f"• {title[:45]}")
        lines.append("")
    
    return "\n".join(lines)


def run_sector_hot_push(check_interval_minutes=30):
    """执行热门板块推送

    Args:
        check_interval_minutes: 检查间隔（分钟），用于判断是否需要新推送
    """
    print("=" * 50)
    print("  Hot Sectors Monitoring")
    print("=" * 50)

    # 确保表存在
    ensure_table()
    ensure_ch_table()

    now = datetime.now()
    check_time = now.strftime('%H:%M')

    # 获取最新交易日 - ClickHouse优先
    try:
        client = get_ch_client()
        
        # 标准化日期 - 先统一格式再比较
        latest_date = None
        
        # 先尝试标准格式日期 (2026-04-XX)
        result2 = client.query("SELECT MAX(trade_date) FROM stock_daily WHERE length(trade_date) = 10")
        if result2.result_rows and result2.result_rows[0][0]:
            latest_date = result2.result_rows[0][0]
            print(f"[DEBUG] CH using standard: {latest_date}")
        
        # 如果没有，尝试紧凑格式
        if not latest_date:
            result = client.query("SELECT MAX(trade_date) FROM stock_daily WHERE length(trade_date) = 8")
            if result.result_rows and result.result_rows[0][0]:
                d = result.result_rows[0][0]
                latest_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                print(f"[DEBUG] CH converted: {latest_date}")
            
    except Exception as e:
        print(f"[CH date] {e}")
        latest_date = None

    if not latest_date:
        # 回退MySQL
        try:
            from src.utils.db_utils import DBUtils
            latest_date = DBUtils.query_df("SELECT MAX(trade_date) as d FROM stock_daily").iloc[0]['d']
        except:
            latest_date = None

    if not latest_date:
        print("No trade data")
        return False

    trade_date = pd.Timestamp(latest_date).strftime('%Y-%m-%d')
    print(f"Date: {trade_date}, Time: {check_time}")

    # 获取上次记录
    prev_sectors = get_prev_hot_sectors(trade_date)
    print(f"Prev sectors: {len(prev_sectors)}")

    # 1. 获取热门概念板块 - 优先AKShare，然后Tushare
    print("\n[1] Fetching hot sectors...")
    hot_concepts = get_hot_concepts_akshare()
    hot_sectors_tushare = {}
    
    if not hot_concepts:
        print("[1] Trying Tushare...")
        hot_sectors_tushare = get_hot_sectors_tushare()
    
    if hot_concepts:
        hot_sectors = hot_concepts
        print(f"Hot concepts (AKShare): {len(hot_sectors)}")
        for sector, data in hot_sectors.items():
            print(f"  - {sector}: {data.get('avg_chg', 0):+.1f}%")
    elif hot_sectors_tushare:
        hot_sectors = hot_sectors_tushare
        print(f"Hot sectors (Tushare): {len(hot_sectors)}")
        for sector, data in hot_sectors.items():
            print(f"  - {sector}: {data.get('avg_chg', 0):+.1f}%")
    else:
        # 回退到传统行业涨幅榜
        print("\n[1] Fallback to industry-based hot sectors...")
        gainers = get_top_gainers(trade_date, top_n=50)
        if gainers.empty:
            print("No gainers data")
            return False
        
        hot_sectors = identify_hot_sectors(gainers, min_stocks=3)
        if not hot_sectors:
            print("No hot sectors")
            return False
        
        print(f"Hot sectors: {len(hot_sectors)}")
        for sector, data in hot_sectors.items():
            print(f"  - {sector}: {data['stock_count']} stocks, {data['avg_chg']:+.1f}%")

    # 3. 检查是否有变化
    has_change, change_reason = check_changes(prev_sectors, hot_sectors)
    print(f"Change: {has_change} ({change_reason})")

    # 4. 获取龙头股和概念成分股
    sector_leaders = {}
    sector_stocks_map = {}  # 存储每个板块的成分股
    
    if hot_concepts:
        # AKShare概念板块 - 获取成分股
        for sector in list(hot_sectors.keys())[:3]:
            stocks_df = get_concept_stocks(sector, limit=10)
            if not stocks_df.empty:
                sector_stocks_map[sector] = stocks_df
                # 取涨幅最高的作为龙头
                if '涨跌幅' in stocks_df.columns:
                    leader = stocks_df.iloc[0]
                    sector_leaders[sector] = {
                        'ts_code': leader.get('代码', leader.get('ts_code', '')),
                        'name': leader.get('名称', leader.get('name', '')),
                        'pct_chg': leader.get('涨跌幅', 0)
                    }
                elif 'pct_chg' in stocks_df.columns:
                    leader = stocks_df.iloc[0]
                    sector_leaders[sector] = {
                        'ts_code': leader.get('ts_code', ''),
                        'name': leader.get('name', ''),
                        'pct_chg': leader.get('pct_chg', 0)
                    }
    else:
        # 回退到行业模式 - 需要获取涨幅榜数据
        gainers = get_top_gainers(trade_date, top_n=50)
        for sector, data in hot_sectors.items():
            if not gainers.empty:
                sector_stocks = gainers[gainers['industry'] == sector].head(3)
                if not sector_stocks.empty:
                    sector_leaders[sector] = sector_stocks.iloc[0].to_dict()

    # 5. 保存数据
    # 先获取相关ETF
    print("\n[5] Fetching related ETFs...")
    sector_etfs = {}
    for sector in list(hot_sectors.keys())[:3]:
        etfs = get_related_etfs(sector)
        if etfs:
            sector_etfs[sector] = etfs
            print(f"  {sector}: {len(etfs)} ETFs")
    
    save_sector_hot(trade_date, check_time, hot_sectors, sector_leaders, sector_etfs)

    # 6. 搜索板块相关新闻
    print("\n[6] Fetching sector news...")
    top_sectors = list(hot_sectors.keys())[:3]
    sector_news = {}

    for sector in top_sectors:
        sector_keywords = [sector]
        if '光电子' in sector:
            sector_keywords.extend(['光学', '电子'])
        if '通信' in sector:
            sector_keywords.extend(['5G', '通信设备'])
        if 'AI' in sector or '人工智能' in sector:
            sector_keywords.extend(['大模型', '算力'])

        news = get_sector_news(sector_keywords, hours=48)
        if news:
            sector_news[sector] = news
            print(f"  {sector}: {len(news)} news")

    # 7. LLM分析
    if has_change:
        print("\n[7] LLM...")
        llm_client = LLMClient()
        
        # 存储各类分析结果
        sector_analysis = {}       # 龙头股分析
        sector_reason = {}         # 板块上涨原因
        sector_recommend = {}      # 推荐股票
        
        if llm_client.is_available() and sector_leaders:
            # 分析热门板块 (最多3个)
            for sector, leader in list(sector_leaders.items())[:3]:
                # 7a. 龙头股分析
                try:
                    sector_analysis[sector] = analyze_leader_stock(leader, llm_client)
                except:
                    pass
                
                # 7b. 板块上涨原因
                try:
                    stocks_df = sector_stocks_map.get(sector, pd.DataFrame())
                    if not stocks_df.empty:
                        sector_reason[sector] = analyze_sector_reason(sector, stocks_df.to_dict('records'), llm_client)
                except:
                    pass
                
                # 7c. 推荐股票及逻辑
                try:
                    stocks_df = sector_stocks_map.get(sector, pd.DataFrame())
                    if not stocks_df.empty:
                        # 转换格式
                        stocks_list = stocks_df.to_dict('records')
                        rec_stocks = recommend_sector_stocks_v2(sector, stocks_list, llm_client, limit=2)
                        if rec_stocks:
                            rec_with_analysis = []
                            for stock in rec_stocks:
                                logic = analyze_stock_recommend(stock, llm_client)
                                rec_with_analysis.append({
                                    'name': stock.get('名称', stock.get('name', '')),
                                    'code': stock.get('代码', stock.get('ts_code', '')),
                                    'chg': stock.get('涨跌幅', stock.get('pct_chg', 0)),
                                    'logic': logic
                                })
                            sector_recommend[sector] = rec_with_analysis
                except Exception as e:
                    print(f"  [Recommend error] {e}")

        content = build_push_message_v2(hot_sectors, sector_analysis, sector_reason, 
                                        sector_recommend, sector_news, trade_date, check_time, sector_etfs)
        title = f"🔥 热门板块分析 {trade_date[5:]} {check_time}"

        success = send_alert(title, content, message_type='morning')

        if success:
            print("[PUSH] Success!")
        else:
            print("[PUSH] Failed")
        return success
    else:
        print("No changes, skip push")
        return True

    return False


if __name__ == '__main__':
    run_sector_hot_push()