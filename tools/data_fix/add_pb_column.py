import duckdb

conn = duckdb.connect('data/quant.db')

# 添加pb列
try:
    conn.execute('ALTER TABLE stock_daily ADD COLUMN pb DOUBLE')
    print("✓ Added pb column to stock_daily")
except Exception as e:
    print(f"pb column already exists or error: {e}")

# 检查stock_daily表结构
result = conn.execute('''
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'stock_daily'
ORDER BY ordinal_position
''').fetchdf()

print("\nstock_daily表结构:")
print(result)

conn.close()
