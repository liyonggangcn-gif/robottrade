import os

# ==========================================
# ⚡️ 网络补丁: 强制禁用系统代理 ⚡️
# 解决 127.0.0.1:7890 Read timed out / ProxyError
# ==========================================
from src.utils.network_utils import clear_proxy_env, patch_requests_no_proxy
clear_proxy_env()
os.environ["no_proxy"] = "*"
patch_requests_no_proxy()

import time
import pandas as pd
import baostock as bs
try:
    import yfinance as yf
except Exception:
    yf = None
from datetime import datetime, timedelta
from tqdm import tqdm
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

try:
    import efinance
except ImportError:
    efinance = None

try:
    import akshare as ak
except ImportError:
    ak = None

try:
    import tushare as ts
except ImportError:
    ts = None

class UniversalDataLoader:
    """通用数据加载器（支持A股和全球市场）"""
    
    def __init__(self):
        """初始化通用数据加载器"""
        # 再次移除代理（模块加载时已清除，此处防御性确保）
        for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            os.environ.pop(k, None)
        # 可选：首次初始化时打印网络状态，便于排查代理问题
        if not getattr(UniversalDataLoader, "_proxy_logged", False):
            proxy_left = [k for k in ("http_proxy", "https_proxy") if os.environ.get(k)]
            print(f"[网络] 代理已禁用，直连 Tushare (api.waditu.com)" + (f" 残留: {proxy_left}" if proxy_left else ""))
            UniversalDataLoader._proxy_logged = True
        
        # 获取配置
        self.tushare_token = Config.tushare_token
        self.start_date = Config.start_date
        
        # 加载股票池配置
        self.global_stocks = self._load_stock_pool_config()
        
        # 初始化Baostock - Temporarily disabled due to hanging
        # try:
        #     lg = bs.login()
        #     if lg.error_code == '0':
        #         print("Successfully initialized Baostock API as fallback")
        #     else:
        #         print(f"Failed to initialize Baostock API: {lg.error_msg}")
        # except Exception as e:
        #     print(f"Failed to initialize Baostock API: {e}")
        
        # 初始化Tushare（如果配置了Token）
        self.pro = None
        if self.tushare_token and ts:
            try:
                # 直接使用token初始化pro_api，避免写入文件
                self.pro = ts.pro_api(token=self.tushare_token)
                print("Successfully initialized Tushare API as tertiary fallback")
            except Exception as e:
                print(f"Failed to initialize Tushare API: {e}")
                self.pro = None
        else:
            print("No Tushare Token configured, tertiary fallback disabled")
        
        # 初始化表结构（使用短连接）
        self._init_tables()
    
    def _init_tables(self):
        """初始化数据库表"""
        # 创建stock_daily表
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS stock_daily (
            trade_date TEXT,
            ts_code TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            vol REAL,
            amount REAL,
            pe_ttm REAL,
            total_mv REAL,
            roe REAL,
            gpr REAL,
            netprofit_yoy REAL,
            PRIMARY KEY (trade_date, ts_code)
        )
        ''')
        print("Successfully initialized stock_daily table")
        
        # 创建stock_info表（如果不存在）
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS stock_info (
            ts_code TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            industry TEXT,
            pe_ttm REAL,
            pb REAL,
            total_mv REAL
        )
        ''')
        print("Successfully initialized stock_info table")
        
        # 创建stock_concepts表（概念/题材映射）
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS stock_concepts (
            ts_code TEXT,
            concept_name TEXT,
            concept_code TEXT,
            PRIMARY KEY (ts_code, concept_name)
        )
        ''')
        print("Successfully initialized stock_concepts table")
        
        # 创建ai_predictions表（AI预测评分）
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS ai_predictions (
            trade_date TEXT,
            ts_code TEXT,
            ai_score REAL,
            PRIMARY KEY (trade_date, ts_code)
        )
        ''')
        print("Successfully initialized ai_predictions table")
        
        # 检查并添加必要的列
        try:
            # 检查是否有market列
            result = DBUtils.query_df('PRAGMA table_info(stock_info)')
            columns = result['name'].tolist()
            
            if 'market' not in columns:
                print("Adding market column to stock_info table...")
                DBUtils.execute('ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS market TEXT')
                print("Successfully added market column to stock_info table")
            else:
                print("stock_info table already has market column")
            
            # 检查是否有基本面数据列
            for column in ['pe_ttm', 'pb', 'total_mv']:
                if column not in columns:
                    print(f"Adding {column} column to stock_info table...")
                    DBUtils.execute(f'ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS {column} REAL')
                    print(f"Successfully added {column} column to stock_info table")
                else:
                    print(f"stock_info table already has {column} column")
            
            # 检查是否有industry列
            if 'industry' not in columns:
                print("Adding industry column to stock_info table...")
                DBUtils.execute('ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS industry TEXT')
                print("Successfully added industry column to stock_info table")
            else:
                print("stock_info table already has industry column")
        except Exception as e:
            print(f"Error checking columns: {e}")
    
    def get_stock_list(self, full_market=True, full_fundamental=False):
        """获取股票列表。已取消关注标的，始终使用 A 股全市场。

        Args:
            full_market: 保留参数兼容，忽略；始终全市场。
            full_fundamental: 是否强制更新全部基本面数据（默认False，只增量更新）

        Returns:
            pandas DataFrame: 股票列表
        """
        # 始终全市场，不再区分关注标的
        print("[全市场] 获取A股全市场股票列表...")
        stock_list = self._get_full_market_stocks()
        print(f"[OK] 成功加载 {len(stock_list)} 只全市场股票")

        # 获取基本面数据（仅对A股）
        stock_list = self._get_fundamental_data(stock_list, full_fundamental=full_fundamental)
        
        # 写入stock_info表
        stock_info_df = stock_list.rename(columns={'code': 'ts_code'})
        
        # 确保所有必要的列都存在
        required_cols = ['ts_code', 'name', 'market', 'industry', 'pe_ttm', 'pb', 'total_mv']
        for col in required_cols:
            if col not in stock_info_df.columns:
                stock_info_df[col] = None
        
        # 写入数据库（不使用to_sql，因为pandas不支持pymysql）
        print(f"[DEBUG] 开始写入stock_info，准备写入 {len(stock_info_df)} 条数据...")
        
        try:
            with DBUtils.get_conn() as conn:
                cursor = conn.cursor()
                
                # 先删除旧数据
                deleted_count = 0
                for _, row in stock_info_df.iterrows():
                    try:
                        cursor.execute(
                            'DELETE FROM stock_info WHERE ts_code = %s',
                            [row['ts_code']]
                        )
                        deleted_count += 1
                    except Exception as e:
                        print(f"✗ Error deleting {row['ts_code']}: {e}")
                
                print(f"[DEBUG] 已删除 {deleted_count} 条旧数据")
                
                # 使用原始SQL INSERT写入数据
                insert_sql = """
                INSERT INTO stock_info (ts_code, name, market, industry, pe_ttm, pb, total_mv)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                
                inserted_count = 0
                for _, row in stock_info_df.iterrows():
                    try:
                        cursor.execute(insert_sql, [
                            row.get('ts_code'),
                            row.get('name'),
                            row.get('market'),
                            row.get('industry'),
                            row.get('pe_ttm'),
                            row.get('pb'),
                            row.get('total_mv')
                        ])
                        inserted_count += 1
                    except Exception as e:
                        print(f"✗ Error inserting {row.get('ts_code')}: {e}")
                
                conn.commit()
                print(f"[DEBUG] 成功写入 {inserted_count} 条数据")
                
                # 查询当前数据库中的记录数
                cursor.execute("SELECT COUNT(*) as cnt FROM stock_info")
                result = cursor.fetchone()
                print(f"[DEBUG] stock_info 表中共有 {result[0]} 条记录")
            
            print(f"Successfully updated stock_info table with {len(stock_list)} records")
        except Exception as e:
            print(f"✗ 写入stock_info失败: {e}")
            import traceback
            traceback.print_exc()
        
        return stock_list
    
    def _get_fundamental_data(self, stock_list, full_fundamental=False):
        """获取股票基本面数据（使用Tushare Pro）
        
        Args:
            stock_list: 股票列表DataFrame
            full_fundamental: 是否强制更新全部基本面数据（默认False，只增量更新）
            
        Returns:
            包含基本面数据的股票列表
        """
        print("获取基本面数据...")
        
        # 初始化基本面数据列
        stock_list['pe_ttm'] = 0.0
        stock_list['pb'] = 0.0
        stock_list['total_mv'] = 0.0
        stock_list['industry'] = ''
        
        # 只对A股获取基本面数据（处理空DataFrame或缺少market列的情况）
        if stock_list.empty or 'market' not in stock_list.columns:
            print("警告: 股票列表为空或缺少market列，跳过基本面数据获取")
            return stock_list
        
        a_share_list = stock_list[stock_list['market'] == 'A']
        if len(a_share_list) == 0:
            print("No A-shares to get fundamental data for")
            return stock_list
        
        # 使用Tushare Pro获取基本面数据
        if self.pro:
            try:
                print("Fetching fundamental data from Tushare Pro...")
                
                # 转换为Tushare格式
                ts_codes = []
                for _, row in a_share_list.iterrows():
                    code = row['code']
                    if code.startswith('6'):
                        ts_codes.append(f"{code}.SH")
                    else:
                        ts_codes.append(f"{code}.SZ")
                
                # 分批获取基本面数据（每次最多100个）
                batch_size = 100
                fundamental_data = {}
                industry_data = {}
                
                # 获取行业信息
                print("Fetching industry information from Tushare Pro...")
                try:
                    df_basic = self.pro.stock_basic(
                        exchange='',
                        list_status='L',
                        fields='ts_code,industry'
                    )
                    if df_basic is not None and not df_basic.empty:
                        for _, row in df_basic.iterrows():
                            industry_data[row['ts_code']] = row['industry']
                    print(f"Successfully fetched industry data for {len(industry_data)} stocks")
                except Exception as e:
                    print(f"Error fetching industry data: {e}")
                
                # 获取每日基本面数据（逐个查询，控制API调用频次）
                batch_size = 150  # 减少每批调用次数，避免触发频率限制
                call_delay = 0.5  # 增加调用间隔，避免触发频率限制
                total = len(ts_codes)
                failed_codes = []
                error_counts = {}
                fundamental_data = {}  # 累积已获取的数据

                # 断点续传：从数据库查询已存在的股票，跳过已获取的
                # 如果 full_fundamental=True，则强制更新全部
                if not full_fundamental:
                    print("[基本面] 检查已有数据，跳过已存在的股票...")
                    try:
                        existing_df = DBUtils.query_df("SELECT ts_code FROM stock_info WHERE pe_ttm IS NOT NULL")
                        existing_codes = set(existing_df['ts_code'].tolist()) if not existing_df.empty else set()
                        print(f"[基本面] 数据库中已有 {len(existing_codes)} 只股票有基本面数据，将跳过这些股票")
                        
                        # 过滤掉已存在的股票
                        ts_codes = [c for c in ts_codes if c not in existing_codes]
                        print(f"[基本面] 剩余需要获取: {len(ts_codes)} 只股票")
                    except Exception as e:
                        print(f"[基本面] 检查已有数据失败，将从头获取: {e}")
                        existing_codes = set()
                else:
                    print("[基本面] 强制全量更新，跳过断点检查")

                if not ts_codes:
                    print("[基本面] 所有股票基本面数据已存在，无需获取")
                    return stock_list
                
                # 开始前提示（便于排查超时/代理问题）
                total = len(ts_codes)
                print(f"[基本面] 开始获取 {total} 只股票的基本面数据（逐个请求，约 150 次/分钟）")
                print("[基本面] 若出现 Read timed out / 127.0.0.1:7890，请关闭 Clash/V2Ray 等代理，或运行前执行: set no_proxy=*")
                
                for idx, ts_code in enumerate(ts_codes, 1):
                    if idx % 50 == 0 or idx == total:
                        print(f"  Progress: {idx}/{total} (成功 {len(fundamental_data)}, 失败 {len(failed_codes)})")
                    
                    # 增加重试机制
                    max_retries = 3
                    retry_count = 0
                    success = False
                    
                    while retry_count < max_retries and not success:
                        try:
                            df_daily = self.pro.daily_basic(
                                ts_code=ts_code,
                                trade_date='',
                                fields='ts_code,trade_date,pe_ttm,pb,total_mv'
                            )
                            
                            if df_daily is not None and not df_daily.empty:
                                row = df_daily.iloc[0]
                                fundamental_data[ts_code] = {
                                    'code': ts_code.replace('.SH', '').replace('.SZ', ''),
                                    'pe_ttm': row['pe_ttm'],
                                    'pb': row['pb'],
                                    'total_mv': row['total_mv']
                                }
                                success = True
                            else:
                                print(f"  ⚠ {ts_code}: No basic data returned, skipping")
                                break
                        except Exception as e:
                            err_msg = str(e)
                            retry_count += 1
                            
                            # 仅前 10 个错误打印详情，避免刷屏；且让原因一目了然
                            if len(failed_codes) <= 10 or retry_count == max_retries:
                                if "127.0.0.1" in err_msg or "7890" in err_msg or "proxy" in err_msg.lower():
                                    action = "→ 请关闭 Clash/V2Ray 等代理，或运行前执行: set no_proxy=*"
                                elif "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
                                    action = "→ 请求超时，可能是代理或网络不稳定，建议关闭代理后重试"
                                elif "最多访问该接口200次" in err_msg or "rate limit" in err_msg.lower():
                                    action = "→ API调用频率限制，正在调整调用间隔..."
                                else:
                                    action = ""
                                short_err = err_msg[:60] + "..." if len(err_msg) > 60 else err_msg
                                print(f"  ✗ {ts_code} (尝试 {retry_count}/{max_retries}): {short_err}")
                                if action:
                                    print(f"    {action}")
                            
                            if retry_count < max_retries:
                                print(f"  重试中... ({retry_count}/{max_retries})")
                                time.sleep(2)  # 重试前等待2秒
                            else:
                                failed_codes.append(ts_code)
                                error_counts[err_msg] = error_counts.get(err_msg, 0) + 1
                    
                    # 控制API调用频次
                    if idx % batch_size == 0 and idx < len(ts_codes):
                        print(f"  Reached {batch_size} calls, waiting 90 seconds...")
                        
                        # 每150条写入一次数据库
                        print(f"[DEBUG] 准备写入已获取的基本面数据 {len(fundamental_data)} 条...")
                        DBUtils._write_fundamental_batch(fundamental_data, industry_data)
                        
                        time.sleep(90)  # 等待90秒后继续，增加等待时间避免频率限制
                    elif idx % 5 == 0:
                        time.sleep(call_delay)  # 每5次调用等待0.5秒，增加等待时间避免频率限制
                
                # 循环结束后再写入一次（如果有剩余）
                if fundamental_data:
                    print(f"[DEBUG] 写入最后一批基本面数据 {len(fundamental_data)} 条...")
                    DBUtils._write_fundamental_batch(fundamental_data, industry_data)
                
                # 更新stock_list中的基本面数据和行业信息
                for idx, row in stock_list.iterrows():
                    code = row['code']
                    if row['market'] == 'A':
                        if code.startswith('6'):
                            ts_code = f"{code}.SH"
                        else:
                            ts_code = f"{code}.SZ"
                        
                        if ts_code in fundamental_data:
                            stock_list.at[idx, 'pe_ttm'] = fundamental_data[ts_code]['pe_ttm']
                            stock_list.at[idx, 'pb'] = fundamental_data[ts_code]['pb']
                            stock_list.at[idx, 'total_mv'] = fundamental_data[ts_code]['total_mv']
                        
                        if ts_code in industry_data:
                            stock_list.at[idx, 'industry'] = industry_data[ts_code]
                        else:
                            stock_list.at[idx, 'industry'] = ''
                
                # 汇总日志（便于快速定位问题）
                ok_count = len(fundamental_data)
                fail_count = len(failed_codes)
                print(f"\n[基本面] 结果: 成功 {ok_count}/{total} 只")
                if fail_count > 0:
                    print(f"  失败 {fail_count} 只，主要错误:")
                    for err, cnt in sorted(error_counts.items(), key=lambda x: -x[1])[:5]:
                        short = err[:70] + "..." if len(err) > 70 else err
                        print(f"    ({cnt}次) {short}")
                    err_str = str(error_counts)
                    if "127.0.0.1" in err_str or "7890" in err_str or "timed out" in err_str.lower():
                        print("  [建议] 若大量超时/代理错误，请关闭 Clash/V2Ray 后重试，或运行前执行: set no_proxy=*")
                print(f"  行业数据已合并: {len([i for i, r in stock_list.iterrows() if r.get('industry')])} 只")
                
            except Exception as e:
                print(f"Error fetching fundamental data from Tushare Pro: {e}")
        else:
            print("Tushare Pro not available, skipping fundamental data")
        
        return stock_list
    
    def _load_stock_pool_config(self):
        """加载股票池配置
        
        Returns:
            list: 全球股票列表
        """
        import yaml
        
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'stock_pool.yaml')
        global_stocks = []
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                if 'global_stocks' in config:
                    global_stocks = config['global_stocks']
                    print(f"Successfully loaded {len(global_stocks)} global stocks from config")
        except Exception as e:
            print(f"Error loading stock pool config: {e}")
            # 兜底股票列表
            global_stocks = ['MSFT', 'AMD', 'NVDA', 'AAPL', 'TSLA', '00700']
        
        return global_stocks
    
    def _get_full_market_stocks(self):
        """获取全市场股票列表
        
        Returns:
            pandas DataFrame: 全市场股票列表
        """
        stocks = []
        
        # 1. 获取A股全市场股票
        try:
            print("获取A股全市场股票...")
            
            # 优先使用Tushare获取A股列表
            if self.pro:
                try:
                    print("使用Tushare Pro获取A股列表...")
                    df = self.pro.stock_basic(
                        exchange='',
                        list_status='L',
                        fields='ts_code, name'
                    )
                    
                    if df is not None and not df.empty:
                        for _, row in df.iterrows():
                            ts_code = row['ts_code']
                            # 提取基础代码（去掉.SH/.SZ后缀）
                            stock_code = ts_code.split('.')[0]
                            name = row['name']
                            
                            # 只保留6位数字代码
                            if stock_code.isdigit() and len(stock_code) == 6:
                                stocks.append({
                                    'code': stock_code,
                                    'name': name,
                                    'market': 'A',
                                    'ts_code': ts_code
                                })
                        print(f"成功通过Tushare获取{len([s for s in stocks if s['market'] == 'A'])}只A股")
                except Exception as e:
                    print(f"Tushare获取A股列表失败: {e}")
            
            # 如果Tushare失败，尝试使用Baostock
            if len([s for s in stocks if s['market'] == 'A']) == 0:
                print("使用Baostock获取A股列表...")
                # 使用Baostock获取A股列表
                today = datetime.now().strftime('%Y-%m-%d')
                rs = bs.query_all_stock(day=today)
                
                if rs and rs.error_code == '0':
                    while rs.next():
                        row = rs.get_row_data()
                        code = row[0]
                        name = row[2]
                        
                        # 提取股票代码
                        if code.startswith('sh.'):
                            stock_code = code[3:]
                            ts_code = f"{stock_code}.SH"
                        elif code.startswith('sz.'):
                            stock_code = code[3:]
                            ts_code = f"{stock_code}.SZ"
                        else:
                            continue
                        
                        # 只保留6位数字代码
                        if stock_code.isdigit() and len(stock_code) == 6:
                            stocks.append({
                                'code': stock_code,
                                'name': name,
                                'market': 'A',
                                'ts_code': ts_code
                            })
                    print(f"成功通过Baostock获取{len([s for s in stocks if s['market'] == 'A'])}只A股")
        except Exception as e:
            print(f"获取A股列表失败: {e}")
        
        # 2. 获取配置的全球股票（美股+港股）
        try:
            print("获取配置的全球股票...")
            for code in self.global_stocks:
                code_str = str(code)
                # 识别市场
                if code_str.isalpha():
                    market = 'US'
                elif code_str.isdigit() and len(code_str) == 5:
                    market = 'HK'
                else:
                    continue
                
                # 简化处理，使用代码作为名称
                stocks.append({
                    'code': code_str,
                    'name': code_str,
                    'market': market
                })
            print(f"成功添加{len([s for s in stocks if s['market'] in ['US', 'HK']])}只全球股票")
        except Exception as e:
            print(f"获取全球股票列表失败: {e}")
        
        return pd.DataFrame(stocks)
    
    def get_latest_date(self, ts_code):
        """获取数据库中某只股票的最新交易日期
        
        Args:
            ts_code: 股票代码
            
        Returns:
            最新交易日期或None
        """
        result = DBUtils.query_df('''
        SELECT MAX(trade_date) as max_date FROM stock_daily WHERE ts_code = ?
        ''', [ts_code])
        
        if not result.empty and pd.notna(result.iloc[0]['max_date']):
            return result.iloc[0]['max_date']
        else:
            return None
    
    def _identify_market(self, code):
        """智能识别股票市场
        
        Args:
            code: 股票代码
            
        Returns:
            market: 'A', 'US', 'HK'
        """
        code_str = str(code)
        
        # A股：6位数字
        if code_str.isdigit() and len(code_str) == 6:
            return 'A'
        
        # 港股：5位数字
        elif code_str.isdigit() and len(code_str) == 5:
            return 'HK'
        
        # 美股：纯字母
        elif code_str.isalpha():
            return 'US'
        
        # 其他情况
        return 'UNKNOWN'
    
    def _fetch_ashare(self, code):
        """获取A股数据（Tushare Pro → eFinance → Baostock 优先级）
        
        Args:
            code: A股代码
            
        Returns:
            pandas DataFrame
        """
        # 获取最新日期，实现增量同步
        latest_date = None
        try:
            latest_date = self.get_latest_date(code)
        except Exception as e:
            print(f"Error getting latest date: {e}")
        
        # 确定开始日期
        if latest_date:
            start_date = (pd.to_datetime(latest_date) + timedelta(days=1)).strftime('%Y%m%d')
        else:
            start_date = self.start_date
        
        # 结束日期
        end_date = datetime.now().strftime('%Y%m%d')
        
        # 跳过无效日期范围
        if start_date > end_date:
            print(f"No new data to fetch for {code}")
            return pd.DataFrame()
        
        # Try 1: Tushare Pro (主要数据源)
        if self.pro:
            try:
                print(f"Fetching data from Tushare Pro for {code}")
                
                # 转换为Tushare格式
                if code.startswith('6'):
                    ts_code = f"{code}.SH"
                else:
                    ts_code = f"{code}.SZ"
                
                # 获取日线数据
                df = self.pro.daily(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date
                )
                
                if df is not None and not df.empty:
                    # 重命名列
                    df = df.rename(columns={
                        'cal_date': 'trade_date',
                        'ts_code': 'ts_code',
                        'open': 'open',
                        'high': 'high',
                        'low': 'low',
                        'close': 'close',
                        'vol': 'vol',
                        'amount': 'amount'
                    })
                    
                    # 转换代码格式
                    df['ts_code'] = ts_code
                    
                    # 获取基本面数据（pe_ttm, pb, total_mv）
                    try:
                        df_basic = self.pro.daily_basic(
                            ts_code=ts_code,
                            start_date=start_date,
                            end_date=end_date,
                            fields='ts_code,trade_date,pe,pb,total_mv'
                        )
                        
                        if df_basic is not None and not df_basic.empty:
                            # 重命名列（pe -> pe_ttm）
                            df_basic = df_basic.rename(columns={
                                'pe': 'pe_ttm'
                            })
                            
                            # 合并基本面数据
                            df = pd.merge(df, df_basic[['trade_date', 'pe_ttm', 'pb', 'total_mv']], 
                                         on='trade_date', how='left')
                            print(f"✓ Merged basic data for {code}")
                        else:
                            print(f"No basic data from Tushare Pro for {code}")
                            # 如果没有基本面数据，返回空DataFrame
                            return pd.DataFrame()
                    except Exception as e:
                        h = " [提示: 若为代理/超时，请关闭代理]" if ("proxy" in str(e).lower() or "timeout" in str(e).lower() or "127.0.0.1" in str(e)) else ""
                        print(f"Error fetching basic data for {code}: {e}{h}")
                        # 如果获取基本面数据失败，返回空DataFrame
                        return pd.DataFrame()
                    
                    # 统一格式
                    df = self._normalize_data(df)
                    
                    # 过滤出增量数据
                    if latest_date:
                        df = df[df['trade_date'] > latest_date]
                    
                    if not df.empty:
                        print(f"Successfully fetched {len(df)} records from Tushare Pro for {code}")
                        return df
                    else:
                        print(f"No new data from Tushare Pro for {code}")
                        return pd.DataFrame()
                else:
                    print(f"No data from Tushare Pro for {code}")
            except Exception as e:
                h = " [提示: 若为代理/超时，请关闭 Clash/V2Ray 或 set no_proxy=*]" if ("proxy" in str(e).lower() or "timeout" in str(e).lower() or "127.0.0.1" in str(e)) else ""
                print(f"Error fetching from Tushare Pro: {e}{h}")
        
        # 备用数据源（eFinance和Baostock）不提供基本面数据，根据要求不使用
        print(f"Skipping backup data sources for {code} (no fundamental data available)")
        
        return pd.DataFrame()
    
    def _fetch_financial_indicators(self, code):
        """获取财务指标数据（ROE, 毛利率, 净利润增长率）
        
        Args:
            code: A股代码
            
        Returns:
            pandas DataFrame: 包含财务指标的数据
        """
        if not self.pro:
            print("Tushare Pro not available, skipping financial indicators")
            return pd.DataFrame()
        
        try:
            print(f"Fetching financial indicators for {code}")
            
            # 转换为Tushare格式
            if code.startswith('6'):
                ts_code = f"{code}.SH"
            else:
                ts_code = f"{code}.SZ"
            
            # 获取财务指标数据
            df_fina = self.pro.fina_indicator(
                ts_code=ts_code,
                fields='ts_code,end_date,roe,grossprofit_margin,netprofit_yoy'
            )
            
            if df_fina is None or df_fina.empty:
                print(f"No financial indicators data for {code}")
                return pd.DataFrame()
            
            # 转换日期格式
            df_fina['end_date'] = pd.to_datetime(df_fina['end_date'])
            
            # 重命名列
            df_fina = df_fina.rename(columns={
                'end_date': 'trade_date',
                'roe': 'roe',
                'grossprofit_margin': 'gpr',
                'netprofit_yoy': 'netprofit_yoy'
            })
            
            # 添加ts_code列
            df_fina['ts_code'] = ts_code
            
            print(f"Successfully fetched {len(df_fina)} financial indicator records for {code}")
            return df_fina
            
        except Exception as e:
            h = " [提示: 若为代理/超时，请关闭代理]" if ("proxy" in str(e).lower() or "timeout" in str(e).lower() or "127.0.0.1" in str(e)) else ""
            print(f"Error fetching financial indicators for {code}: {e}{h}")
            return pd.DataFrame()
    
    def _fetch_global(self, code):
        """获取全球市场数据（YFinance）
        
        Args:
            code: 股票代码
            
        Returns:
            pandas DataFrame
        """
        max_retries = 3
        base_delay = 2
        
        for attempt in range(max_retries):
            try:
                print(f"Fetching data from YFinance for {code} (attempt {attempt+1}/{max_retries})")
                
                # 处理代码后缀
                code_str = str(code)
                if code_str.isdigit() and len(code_str) == 5:
                    # 港股
                    yf_code = f"{code_str[1:]}.HK" if code_str.startswith('0') else f"{code_str}.HK"
                else:
                    # 美股
                    yf_code = code_str
                
                # 添加延迟以避免限流
                if attempt > 0:
                    delay = base_delay * (2 ** attempt)
                    print(f"Waiting {delay} seconds before retry...")
                    time.sleep(delay)
                
                # 获取数据
                df = yf.download(
                    yf_code,
                    start=pd.to_datetime(self.start_date, format='%Y%m%d'),
                    end=datetime.now(),
                    auto_adjust=False,
                    threads=False  # 禁用多线程以减少请求频率
                )
                
                if not df.empty:
                    # 重置索引
                    df = df.reset_index()
                    
                    # 处理时区问题
                    if df['Date'].dt.tz is not None:
                        df['Date'] = df['Date'].dt.tz_localize(None)
                    
                    # 重命名列
                    df = df.rename(columns={
                        "Date": "trade_date",
                        "Open": "open",
                        "High": "high",
                        "Low": "low",
                        "Close": "close",
                        "Volume": "vol"
                    })
                    
                    # 添加ts_code
                    df['ts_code'] = code
                    
                    # 添加amount（如果没有）
                    if 'amount' not in df.columns:
                        df['amount'] = df['close'] * df['vol']
                    
                    # 统一格式
                    df = self._normalize_data(df)
                    print(f"Successfully fetched {len(df)} records from YFinance for {code}")
                    return df
            except Exception as e:
                error_msg = str(e)
                print(f"Error fetching from YFinance: {e}")
                
                # 检查是否是限流错误
                if "Too Many Requests" in error_msg or "Rate limited" in error_msg:
                    if attempt < max_retries - 1:
                        print("Rate limited, will retry...")
                        continue
                    else:
                        print("Max retries reached for rate limiting")
                break
        
        return pd.DataFrame()
    
    def _normalize_data(self, df):
        """统一数据格式
        
        Args:
            df: 原始DataFrame
            
        Returns:
            标准化后的DataFrame
        """
        # 确保包含标准字段
        required_cols = ['trade_date', 'ts_code', 'open', 'high', 'low', 'close', 'vol']
        optional_cols = ['amount', 'pe_ttm', 'total_mv', 'pb', 'roe', 'gpr', 'netprofit_yoy']
        
        # 添加缺失字段
        for col in required_cols + optional_cols:
            if col not in df.columns:
                df[col] = None
        
        # 处理日期格式 - 保持为 datetime 类型，不转换为字符串
        if 'trade_date' in df.columns:
            df['trade_date'] = pd.to_datetime(df['trade_date'])
        
        # 处理数值类型
        numeric_cols = ['open', 'high', 'low', 'close', 'vol', 'amount', 'pe_ttm', 'total_mv', 'pb', 'roe', 'gpr', 'netprofit_yoy']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 按日期排序
        if 'trade_date' in df.columns:
            df = df.sort_values('trade_date')
        
        # 保留所有字段
        return df[required_cols + optional_cols]
    
    def sync_daily_data(self, limit=None, batch_size=100, full_market=True, max_retries=1, full_fundamental=False):
        """同步日线数据（快速批量模式），支持失败重试并返回同步摘要。

        Args:
            limit: 限制同步的股票数量，None表示全部
            batch_size: 每批同步的股票数量（快速模式下被忽略）
            full_market: 是否同步全市场股票（默认True）
            max_retries: 整体失败时重试次数（默认1，即最多执行2次）
            full_fundamental: 是否强制更新全部基本面数据（默认False，只增量更新）

        Returns:
            dict: {success, total_stocks, success_count, fail_count, total_inserted, error}
        """
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    print(f"[RETRY] 行情同步第 {attempt + 1} 次重试...")
                    time.sleep(5)
                result = self._sync_daily_data_fast(full_market=full_market)
                result["success"] = True
                result["error"] = None
                return result
            except Exception as e:
                last_error = e
                print(f"[WARN] 行情同步异常: {e}")
                continue
        return {
            "success": False,
            "total_stocks": 0,
            "success_count": 0,
            "fail_count": 0,
            "total_inserted": 0,
            "error": str(last_error)[:200],
        }

    def _sync_daily_data_fast(self, full_market=True):
        """快速批量同步日线数据 - 使用Tushare批量API代替逐只股票查询"""
        import pandas as pd
        from datetime import datetime, timedelta
        
        print("[快速同步] 使用批量API模式")
        
        # 1. 同步股票名称（首次或定期）
        print("[快速同步] 同步股票名称...")
        try:
            df_basic = self.pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,market')
            if df_basic is not None and not df_basic.empty:
                with DBUtils.get_conn() as conn:
                    cursor = conn.cursor()
                    count = 0
                    for _, r in df_basic.iterrows():
                        try:
                            cursor.execute(
                                "INSERT INTO stock_info (ts_code, name, market) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE name=VALUES(name)",
                                [r['ts_code'], r['name'], r.get('market', 'A')]
                            )
                            count += 1
                        except:
                            pass
                print(f"[快速同步] 股票名称更新完成: {count}条")
        except Exception as e:
            print(f"[快速同步] 股票名称同步失败: {e}")

        # 2. 获取需要同步的日期
        today = datetime.now().strftime('%Y-%m-%d')
        try:
            df_dates = DBUtils.query_df("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 5")
            known_dates = set(df_dates['trade_date'].tolist()) if not df_dates.empty else set()
        except:
            known_dates = set()
        
        # 获取交易日历
        missing_dates = []
        try:
            start = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')
            end = datetime.now().strftime('%Y%m%d')
            df_cal = self.pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
            if df_cal is not None and not df_cal.empty:
                for d in df_cal['cal_date'].tolist():
                    date_str = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                    if date_str not in known_dates and date_str <= today:
                        missing_dates.append(date_str)
        except:
            pass
        
        if not missing_dates:
            print("[快速同步] 无需同步的新日期")
            return {"total_stocks": 0, "success_count": 0, "total_inserted": 0}
        
        print(f"[快速同步] 需要同步日期: {missing_dates}")
        
        total_inserted = 0
        success_count = 0
        
        # 3. 批量同步每个日期
        for trade_date in missing_dates:
            td = trade_date.replace("-", "")
            
            # 获取全市场OHLCV
            df_daily = None
            for attempt in range(3):
                try:
                    df_daily = self.pro.daily(trade_date=td)
                    if df_daily is not None and not df_daily.empty:
                        break
                except Exception as e:
                    print(f"  重试 {attempt+1}: {e}")
                    time.sleep(10)
            
            if df_daily is None or df_daily.empty:
                print(f"  {trade_date}: 无行情数据")
                continue
            
            # 获取基本面数据
            df_basic = None
            try:
                df_basic = self.pro.daily_basic(trade_date=td, fields="ts_code,pe_ttm,pb,total_mv")
            except:
                pass
            
            # 合并数据
            df_merged = df_daily.copy()
            if df_basic is not None and not df_basic.empty:
                df_merged = df_merged.merge(df_basic[["ts_code", "pe_ttm", "pb", "total_mv"]], on="ts_code", how="left")
            else:
                for col in ["pe_ttm", "pb", "total_mv"]:
                    df_merged[col] = None
            
            df_merged["trade_date"] = trade_date
            
            # 批量插入
            with DBUtils.get_conn() as conn:
                cursor = conn.cursor()
                insert_sql = """
                INSERT INTO stock_daily 
                    (trade_date, ts_code, open, high, low, close, vol, amount, pe_ttm, total_mv)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    open=VALUES(open), high=VALUES(high), low=VALUES(low),
                    close=VALUES(close), vol=VALUES(vol), amount=VALUES(amount),
                    pe_ttm=VALUES(pe_ttm), total_mv=VALUES(total_mv)
                """
                count = 0
                for _, r in df_merged.iterrows():
                    try:
                        cursor.execute(insert_sql, [
                            r["trade_date"], r["ts_code"],
                            None if pd.isna(r.get("open")) else float(r["open"]),
                            None if pd.isna(r.get("high")) else float(r["high"]),
                            None if pd.isna(r.get("low")) else float(r["low"]),
                            None if pd.isna(r.get("close")) else float(r["close"]),
                            None if pd.isna(r.get("vol")) else float(r["vol"]),
                            None if pd.isna(r.get("amount")) else float(r["amount"]),
                            None if pd.isna(r.get("pe_ttm")) else float(r["pe_ttm"]),
                            None if pd.isna(r.get("total_mv")) else float(r["total_mv"]),
                        ])
                        count += 1
                    except:
                        pass
                
                # 更新stock_info
                if df_basic is not None and not df_basic.empty:
                    upsert_sql = """
                    INSERT INTO stock_info (ts_code, pe_ttm, pb, total_mv)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE pe_ttm=VALUES(pe_ttm), pb=VALUES(pb), total_mv=VALUES(total_mv)
                    """
                    for _, r in df_basic.iterrows():
                        try:
                            cursor.execute(upsert_sql, [
                                r["ts_code"],
                                None if pd.isna(r.get("pe_ttm")) else float(r["pe_ttm"]),
                                None if pd.isna(r.get("pb")) else float(r["pb"]),
                                None if pd.isna(r.get("total_mv")) else float(r["total_mv"]),
                            ])
                        except:
                            pass
            
            print(f"  {trade_date}: {count}条")
            total_inserted += count
            success_count += 1
        
        print(f"[快速同步] 完成: {success_count}天, {total_inserted}条")
        return {"total_stocks": 5500, "success_count": success_count, "total_inserted": total_inserted}

    def _sync_daily_data_impl(self, limit=None, batch_size=100, full_market=True, full_fundamental=False):
        """同步日线数据内部实现（供 sync_daily_data 调用与重试）。
        
        Args:
            full_fundamental: 是否强制更新全部基本面数据（默认False，只增量更新）
        """
        # 获取股票列表
        stock_list = self.get_stock_list(full_market=full_market, full_fundamental=full_fundamental)
        if limit:
            stock_list = stock_list.head(limit)
        
        # 断点续传：检查已同步的股票，跳过已有日线数据的
        print("[日线] 检查已有日线数据，跳过已存在的股票...")
        try:
            trade_date = datetime.now().strftime('%Y-%m-%d')
            existing_df = DBUtils.query_df(f"SELECT DISTINCT ts_code FROM stock_daily WHERE trade_date = '{trade_date}'")
            existing_codes = set(existing_df['ts_code'].tolist()) if not existing_df.empty else set()
            print(f"[日线] 今日已有 {len(existing_codes)} 只股票有日线数据，将跳过这些股票")
            
            # 过滤掉已存在的股票
            stock_list = stock_list[~stock_list['ts_code'].isin(existing_codes)]
            print(f"[日线] 剩余需要同步: {len(stock_list)} 只股票")
        except Exception as e:
            print(f"[日线] 检查已有数据失败，将从头获取: {e}")
        
        if stock_list.empty:
            print("[日线] 所有股票日线数据已存在，无需同步")
            return
        
        total_stocks = len(stock_list)
        total_inserted = 0
        success_count = 0
        fail_count = 0

        print(f"Syncing daily data for {total_stocks} stocks in batches of {batch_size}")

        for i in range(0, total_stocks, batch_size):
            batch = stock_list.iloc[i : i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_stocks + batch_size - 1) // batch_size
            print(f"\n=== Processing batch {batch_num}/{total_batches} ({len(batch)} stocks) ===")

            for _, row in tqdm(batch.iterrows(), total=len(batch), desc=f"Batch {batch_num}"):
                code = row["code"]
                name = row["name"]
                market = row["market"]
                try:
                    if market == "A":
                        df = self._fetch_ashare(code)
                        if not df.empty:
                            df_fina = self._fetch_financial_indicators(code)
                            if not df_fina.empty:
                                df["trade_date"] = df["trade_date"].astype("datetime64[ns]")
                                df_fina["trade_date"] = df_fina["trade_date"].astype("datetime64[ns]")
                                df = pd.merge_asof(
                                    df.sort_values("trade_date"),
                                    df_fina.sort_values("trade_date"),
                                    on="trade_date",
                                    by="ts_code",
                                    direction="backward",
                                )
                                print(f"✓ Merged financial indicators for {code}")
                    else:
                        df = self._fetch_global(code)

                    if not df.empty:
                        if "trade_date" in df.columns:
                            df["trade_date"] = df["trade_date"].apply(
                                lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) and isinstance(x, pd.Timestamp) else x
                            )
                        required_cols = ["trade_date", "ts_code", "open", "high", "low", "close", "vol"]
                        for col in required_cols:
                            if col not in df.columns:
                                df[col] = None
                        optional_cols = ["amount", "pe_ttm", "total_mv", "roe", "gpr", "netprofit_yoy"]
                        for col in optional_cols:
                            if col not in df.columns:
                                df[col] = None
                        with DBUtils.get_conn() as conn:
                            cursor = conn.cursor()
                            for _, r in df.iterrows():
                                try:
                                    cursor.execute(
                                        "DELETE FROM stock_daily WHERE trade_date = %s AND ts_code = %s",
                                        [r.get("trade_date"), r.get("ts_code")],
                                    )
                                except Exception as e:
                                    print(f"✗ Error deleting duplicate record: {e}")
                            
                            # 使用原始SQL INSERT（pandas to_sql不支持pymysql）
                            insert_sql = """
                            INSERT INTO stock_daily (trade_date, ts_code, open, high, low, close, vol, amount, pe_ttm, total_mv, roe, gpr, netprofit_yoy)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """
                            for _, r in df.iterrows():
                                try:
                                    # 将NaN值转换为None
                                    values = [
                                        r.get("trade_date"), r.get("ts_code"),
                                        None if pd.isna(r.get("open")) else r.get("open"),
                                        None if pd.isna(r.get("high")) else r.get("high"),
                                        None if pd.isna(r.get("low")) else r.get("low"),
                                        None if pd.isna(r.get("close")) else r.get("close"),
                                        None if pd.isna(r.get("vol")) else r.get("vol"),
                                        None if pd.isna(r.get("amount")) else r.get("amount"),
                                        None if pd.isna(r.get("pe_ttm")) else r.get("pe_ttm"),
                                        None if pd.isna(r.get("total_mv")) else r.get("total_mv"),
                                        None if pd.isna(r.get("roe")) else r.get("roe"),
                                        None if pd.isna(r.get("gpr")) else r.get("gpr"),
                                        None if pd.isna(r.get("netprofit_yoy")) else r.get("netprofit_yoy")
                                    ]
                                    cursor.execute(insert_sql, values)
                                except Exception as e:
                                    pass  # 忽略重复插入错误
                            conn.commit()
                            
                            # 查询当前数据库中的记录数
                            cursor.execute("SELECT COUNT(*) as cnt FROM stock_daily")
                            result = cursor.fetchone()
                            print(f"✓ Inserted {len(df)} records for {code}, stock_daily表共有 {result[0]} 条")
                        
                        inserted_count = len(df)
                        total_inserted += inserted_count
                        success_count += 1
                    else:
                        print(f"✗ All data sources failed for {code} ({name}), skipping")
                        fail_count += 1
                except Exception as e:
                    print(f"✗ Error syncing data for {code} ({name}): {e}")
                    fail_count += 1
                    continue

            if i + batch_size < total_stocks:
                print("Taking a short break to avoid API rate limiting...")
                time.sleep(2)

        print(f"\n=== Sync Summary ===")
        print(f"Total stocks: {total_stocks}")
        print(f"Success: {success_count}")
        print(f"Failed: {fail_count}")
        print(f"Total inserted: {total_inserted} records")
        
        # 运行数据质量检查
        print("\n=== 数据质量检查 ===")
        try:
            from src.collector.data_quality import DataQualityChecker
            checker = DataQualityChecker()
            report = checker.generate_quality_report()
            print(report)
            
            # 保存质量报告到文件
            report_path = f"data_quality_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"\n质量报告已保存到: {report_path}")
        except Exception as e:
            print(f"数据质量检查失败: {e}")
        
        return {
            "total_stocks": total_stocks,
            "success_count": success_count,
            "fail_count": fail_count,
            "total_inserted": total_inserted,
        }
    
    def sync_concepts(self, max_retries=1):
        """同步概念/题材映射（Tushare），失败时自动重试一次。"""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                print("[RETRY] 概念同步重试...")
                time.sleep(5)
            try:
                if self._sync_concepts_impl():
                    return True
            except Exception as e:
                print(f"[WARN] 概念同步异常: {e}")
                if attempt == max_retries:
                    import traceback
                    traceback.print_exc()
        return False

    def _sync_concepts_impl(self):
        """概念同步内部实现（供 sync_concepts 重试调用）。"""
        if not self.pro:
            print("[ERROR] Tushare Pro API not available. Cannot sync concepts.")
            return False
        print("\n" + "=" * 60)
        print("  Syncing Stock Concepts from Tushare")
        print("=" * 60)
        # Step 1
        print("[Step 1] Fetching concept list...")
        df_concepts = self.pro.concept(src='ts')
        if df_concepts is None or df_concepts.empty:
            print("[ERROR] No concepts returned from Tushare API")
            return False
        total_concepts = len(df_concepts)
        print(f"[OK] Found {total_concepts} concepts")
        # Step 2
        print("[Step 2] Fetching concept details (this may take a while)...")
        all_mappings = []
        api_call_count = 0
        success_count = 0
        fail_count = 0
        for idx, row in df_concepts.iterrows():
            concept_code = row['code']
            concept_name = row['name']
            try:
                api_call_count += 1
                if api_call_count > 0 and api_call_count % 190 == 0:
                    print("  Reached 190 calls, waiting 60 seconds for rate limit...")
                    time.sleep(60)
                df_detail = self.pro.concept_detail(id=concept_code, fields='ts_code,name')
                if df_detail is not None and not df_detail.empty:
                    for _, detail_row in df_detail.iterrows():
                        all_mappings.append({
                            'ts_code': detail_row['ts_code'],
                            'concept_name': concept_name,
                            'concept_code': concept_code
                        })
                    success_count += 1
                else:
                    fail_count += 1
                time.sleep(0.3)
                if (idx + 1) % 50 == 0:
                    print(f"  Progress: {idx + 1}/{total_concepts} concepts processed")
            except Exception as e:
                fail_count += 1
                error_msg = str(e)[:100] + "..." if len(str(e)) > 100 else str(e)
                print(f"  Error fetching concept {concept_name} ({concept_code}): {error_msg}")
                continue
        print(f"\n  Fetch complete: {success_count} success, {fail_count} failed")
        if not all_mappings:
            print("[WARN] No concept mappings collected")
            return False
        # Step 3
        print(f"[Step 3] Saving {len(all_mappings)} concept mappings to database...")
        df_mappings = pd.DataFrame(all_mappings).drop_duplicates(subset=['ts_code', 'concept_name'])
        print(f"         After dedup: {len(df_mappings)} unique mappings")
        
        # 只保留表中存在的列
        df_mappings = df_mappings[['ts_code', 'concept_name']]
        
        with DBUtils.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM stock_concepts")
            
            # 使用原始SQL INSERT
            insert_sql = "INSERT INTO stock_concepts (ts_code, concept_name) VALUES (%s, %s)"
            for _, r in df_mappings.iterrows():
                try:
                    cursor.execute(insert_sql, [r.get('ts_code'), r.get('concept_name')])
                except Exception as e:
                    pass
            conn.commit()
        unique_stocks = df_mappings['ts_code'].nunique()
        unique_concepts = df_mappings['concept_name'].nunique()
        print(f"[OK] Concept sync complete: {len(df_mappings)} mappings, {unique_stocks} stocks, {unique_concepts} concepts")
        return True

    def close(self):
        """关闭资源"""
        try:
            # bs.logout()
            # print("Successfully logged out from Baostock")
            pass
        except Exception as e:
            print(f"Failed to logout from Baostock: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False  # 不吞掉异常
    
    def __del__(self):
        """析构函数，确保连接被关闭"""
        self.close()


# 保留原有的DataLoader类用于向后兼容
class DataLoader(UniversalDataLoader):
    """向后兼容的DataLoader类"""
    pass
