import sqlite3
import pandas as pd
import os
import time
import re
import random
import threading
from contextlib import contextmanager
from src.utils.config_loader import Config

# 线程级 MySQL 连接缓存：每个线程复用同一个连接，避免反复握手
_mysql_thread_local = threading.local()


class _MySQLCursorWrapper:
    """MySQL cursor wrapper that converts SQLite-style SQL to MySQL-compatible SQL."""
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        sql = DBUtils._convert_sql(sql)
        if params is not None:
            return self._cursor.execute(sql, params)
        return self._cursor.execute(sql)

    def executemany(self, sql, params_list):
        sql = DBUtils._convert_sql(sql)
        return self._cursor.executemany(sql, params_list)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _MySQLConnWrapper:
    """MySQL connection wrapper that returns _MySQLCursorWrapper from cursor()."""
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _MySQLCursorWrapper(self._conn.cursor())

    def __getattr__(self, name):
        return getattr(self._conn, name)


class DBUtils:
    """
    数据库连接管理器
    支持 SQLite 和 MySQL (MariaDB)
    """
    
    _sqlite_path = None
    _mysql_conn = None
    _is_mysql = False
    
    @staticmethod
    def _get_db_path():
        if DBUtils._sqlite_path:
            return DBUtils._sqlite_path
        
        base_path = Config.duckdb_path.replace('.duckdb', '.db')
        if not base_path.endswith('.db'):
            base_path = os.path.splitext(base_path)[0] + '.db'
        if not os.path.isabs(base_path):
            base_path = os.path.join(Config.project_root, base_path)
        os.makedirs(os.path.dirname(base_path), exist_ok=True)
        DBUtils._sqlite_path = base_path
        return base_path
    
    @staticmethod
    def _get_mysql_config():
        if not hasattr(Config, 'db_type') or Config.db_type != 'mysql':
            return None
        
        if not hasattr(Config, 'mysql'):
            return None
        
        return Config.mysql
    
    @staticmethod
    def _new_mysql_conn(mysql_config):
        """创建一个全新的 MySQL 连接"""
        import pymysql
        return pymysql.connect(
            host=mysql_config.get('host', 'localhost'),
            port=mysql_config.get('port', 3306),
            user=mysql_config.get('user', 'root'),
            password=mysql_config.get('password', ''),
            database=mysql_config.get('database', 'quant_trade'),
            charset='utf8mb4',
            init_command="SET NAMES utf8mb4 COLLATE utf8mb4_general_ci",
            autocommit=False,
            read_timeout=600,
            write_timeout=300,
            connect_timeout=15,
        )

    @staticmethod
    def _get_mysql_conn(retries: int = 3, retry_delay: float = 5.0):
        """获取 MySQL 连接（线程复用，避免重复握手）"""
        mysql_config = DBUtils._get_mysql_config()
        if not mysql_config:
            return None
        try:
            import pymysql
        except ImportError:
            print("[WARN] pymysql未安装，使用SQLite")
            return None

        # 尝试复用线程缓存的连接
        cached = getattr(_mysql_thread_local, 'conn', None)
        if cached is not None:
            try:
                cached.ping(reconnect=True)
                return cached
            except Exception:
                try:
                    cached.close()
                except Exception:
                    pass
                _mysql_thread_local.conn = None

        # 新建连接（指数退避：1s → 2s → 4s + jitter）
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                conn = DBUtils._new_mysql_conn(mysql_config)
                _mysql_thread_local.conn = conn
                return conn
            except Exception as e:
                last_err = e
                if attempt < retries:
                    wait = retry_delay * (2 ** (attempt - 1)) + random.uniform(-0.3, 0.3)
                    time.sleep(max(0.1, wait))
        print(f"[WARN] MySQL连接失败(共{retries}次): {last_err}, 使用SQLite")
        return None
    
    @staticmethod
    def _is_mysql_mode():
        """检查是否使用MySQL模式"""
        if not hasattr(Config, 'db_type'):
            return False
        return Config.db_type == 'mysql'
    
    @staticmethod
    def _convert_sql(sql):
        """转换SQL语法以适配MySQL"""
        sql = sql.strip()

        # PRAGMA table_info(table_name) -> information_schema query
        pragma_match = re.match(r"PRAGMA\s+table_info\s*\(\s*(\w+)\s*\)", sql, re.IGNORECASE)
        if pragma_match:
            table_name = pragma_match.group(1)
            return f"SELECT COLUMN_NAME as name FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='{table_name}'"

        # 转换SQLite的 ? 占位符为 %s (MySQL)
        sql = sql.replace('?', '%s')

        # 仅对 DDL 语句做以下转换 (DML 语句不含 AUTOINCREMENT)
        if re.search(r'\bCREATE\s+TABLE\b', sql, re.IGNORECASE):
            # SQLite: INTEGER PRIMARY KEY AUTOINCREMENT → MySQL: INT AUTO_INCREMENT PRIMARY KEY
            sql = re.sub(
                r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
                'INT AUTO_INCREMENT PRIMARY KEY',
                sql, flags=re.IGNORECASE
            )
            # TEXT DEFAULT 'value' → VARCHAR(255) DEFAULT 'value'
            # (MySQL TEXT/BLOB 列不支持 DEFAULT 值)
            sql = re.sub(
                r'\bTEXT(\s+NOT\s+NULL)?\s+DEFAULT\s+(\'[^\']*\'|\d+)',
                lambda m: f"VARCHAR(255){m.group(1) or ''} DEFAULT {m.group(2)}",
                sql, flags=re.IGNORECASE
            )
            # 统一字符集：避免不同表的 ts_code 列 collation 不同导致 MySQL 1267 错误
            # 只有在末尾右括号之后没有 CHARSET/CHARACTER SET 时才追加
            if not re.search(r'(CHARSET|CHARACTER\s+SET)\s*=?\s*utf8mb4', sql, re.IGNORECASE):
                sql = sql.rstrip().rstrip(';')
                sql += ' DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'

        return sql

    @staticmethod
    @contextmanager
    def get_conn(read_only=False):
        if DBUtils._is_mysql_mode():
            # MySQL模式：复用线程连接，不 close（由线程持有）
            conn = DBUtils._get_mysql_conn()
            if conn:
                wrapped = _MySQLConnWrapper(conn)
                try:
                    yield wrapped
                    conn.commit()
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    # 连接可能已损坏，清除缓存让下次重建
                    _mysql_thread_local.conn = None
                    raise e
                return
        
        # SQLite模式
        db_path = DBUtils._get_db_path()
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=60.0, isolation_level='DEFERRED')
            
            # 检查并设置WAL模式
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode;")
            current_mode = cursor.fetchone()[0]
            
            if current_mode != 'wal':
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
            
            yield conn
            conn.commit()
        except sqlite3.DatabaseError as e:
            if conn:
                conn.rollback()
            raise e
        except Exception as e:
            if conn:
                conn.rollback()
            raise e
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _query_df_mysql(conn, sql, params):
        """在给定连接上执行查询并返回 DataFrame（内部辅助）"""
        from decimal import Decimal
        cursor = conn.cursor()
        converted = DBUtils._convert_sql(sql)
        cursor.execute(converted, params) if params else cursor.execute(converted)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        # MySQL DECIMAL 列返回 decimal.Decimal，统一转 float 避免与 float 运算崩溃
        if rows:
            rows = [
                tuple(float(v) if isinstance(v, Decimal) else v for v in row)
                for row in rows
            ]
        return pd.DataFrame(rows, columns=columns)

    @staticmethod
    def query_df(sql, params=None):
        # 转换SQL语法
        if DBUtils._is_mysql_mode():
            conn = DBUtils._get_mysql_conn()
            if conn:
                try:
                    return DBUtils._query_df_mysql(conn, sql, params)
                except Exception:
                    # 连接损坏（如 2013 Lost connection）→ 清缓存并重试一次
                    _mysql_thread_local.conn = None
                    conn2 = DBUtils._get_mysql_conn()
                    if conn2:
                        return DBUtils._query_df_mysql(conn2, sql, params)
                    raise

        # SQLite模式（pymysql未安装时的降级，保留原始 ? 占位符）
        with DBUtils.get_conn() as conn:
            return pd.read_sql_query(sql, conn, params=params)

    @staticmethod
    def execute(sql, params=None):
        # 转换SQL语法
        if DBUtils._is_mysql_mode():
            conn = DBUtils._get_mysql_conn()
            if conn:
                converted = DBUtils._convert_sql(sql)
                try:
                    cursor = conn.cursor()
                    cursor.execute(converted, params) if params else cursor.execute(converted)
                    conn.commit()
                    return cursor.rowcount
                except Exception:
                    # 连接损坏 → 清缓存并重试一次
                    _mysql_thread_local.conn = None
                    conn2 = DBUtils._get_mysql_conn()
                    if conn2:
                        cursor2 = conn2.cursor()
                        cursor2.execute(converted, params) if params else cursor2.execute(converted)
                        conn2.commit()
                        return cursor2.rowcount
                    raise

        # SQLite模式
        with DBUtils.get_conn() as conn:
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            return cursor.rowcount
    
    @staticmethod
    def _write_fundamental_batch(fundamental_data, industry_data):
        """批量写入基本面数据到stock_info表"""
        if not fundamental_data:
            return
        
        try:
            with DBUtils.get_conn() as conn:
                cursor = conn.cursor()
                
                # 先清理没有后缀的旧数据（避免重复）
                cursor.execute("DELETE FROM stock_info WHERE ts_code NOT LIKE '%.%'")
                deleted = cursor.rowcount
                if deleted > 0:
                    print(f"[DEBUG] 已清理 {deleted} 条无后缀旧数据")
                
                insert_sql = """
                INSERT INTO stock_info (ts_code, name, market, industry, pe_ttm, pb, total_mv)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    industry = VALUES(industry), 
                    pe_ttm = VALUES(pe_ttm), 
                    pb = VALUES(pb), 
                    total_mv = VALUES(total_mv)
                """
                
                inserted = 0
                for ts_code, data in fundamental_data.items():
                    code = data.get('code', '')
                    if not code:
                        continue
                    
                    # 获取行业信息
                    ts_full = code + '.SH' if code.startswith('6') else code + '.SZ'
                    industry = industry_data.get(ts_full, '')
                    
                    try:
                        cursor.execute(insert_sql, [
                            ts_full,
                            '',  # name
                            'A',
                            industry,
                            data.get('pe_ttm'),
                            data.get('pb'),
                            data.get('total_mv')
                        ])
                        inserted += 1
                    except Exception as e:
                        pass
                
                conn.commit()
                print(f"[DEBUG] 成功写入/更新 {inserted} 条基本面数据")
                
                # 查询当前记录数
                cursor.execute("SELECT COUNT(*) as cnt FROM stock_info")
                result = cursor.fetchone()
                print(f"[DEBUG] stock_info 表中共有 {result[0]} 条记录")
                
        except Exception as e:
            print(f"[DEBUG] 写入基本面数据失败: {e}")
