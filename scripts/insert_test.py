import sys
sys.path.insert(0, '.')
from src.utils.db_utils import DBUtils

today = '20260416'
DBUtils.execute('DELETE FROM daily_picks WHERE trade_date = %s', (today,))
DBUtils.execute('INSERT INTO daily_picks (trade_date, ts_code, name, final_score, track) VALUES (%s, %s, %s, %s, %s)',
             (today, '000001.SZ', 'Ping An', 0.85, 'test'))
print('Inserted test record')

df = DBUtils.query_df('SELECT * FROM daily_picks WHERE trade_date = %s', (today,))
print('Result:', df.to_string())