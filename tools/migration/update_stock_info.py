from src.collector.data_loader import UniversalDataLoader
from src.utils.db_utils import DBUtils

print("检查 stock_info 表数据...")
df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_info")
print(f"stock_info 表共有 {df.iloc[0,0]} 条记录")

print("\n检查 stock_info 表数据样本...")
df = DBUtils.query_df("SELECT ts_code, name, pe_ttm, total_mv FROM stock_info LIMIT 10")
print(df)

print("\n检查有多少股票有有效的 pe_ttm 和 total_mv...")
df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_info WHERE pe_ttm > 0 AND total_mv > 0")
print(f"有有效数据的股票数量: {df.iloc[0,0]}")

print("\n尝试更新 stock_info 表...")
loader = UniversalDataLoader()
loader.load_stock_list()

print("\n更新后检查...")
df = DBUtils.query_df("SELECT ts_code, name, pe_ttm, total_mv FROM stock_info LIMIT 10")
print(df)

df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_info WHERE pe_ttm > 0 AND total_mv > 0")
print(f"更新后有有效数据的股票数量: {df.iloc[0,0]}")
