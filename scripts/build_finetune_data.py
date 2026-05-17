"""
准备微调数据 - 构建选股训练集
为后续Agent微调准备高质量数据
"""
import json
from datetime import datetime, timedelta
from loguru import logger

from src.utils.db_utils import DBUtils


class FinetuneDataBuilder:
    """微调数据构建器"""

    def __init__(self):
        self.output_dir = "data/finetune/"

    def build_training_data(self, days: int = 90) -> list:
        """构建训练数据
        
        格式：
        {"messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
        ]}
        """
        # 获取选股结果
        df_picks = DBUtils.query_df(f"""
            SELECT dp.trade_date, dp.ts_code, dp.name, dp.final_score, dp.track
            FROM daily_picks dp
            WHERE dp.trade_date >= ?
            ORDER BY dp.trade_date DESC
            LIMIT 100
        """, (datetime.now() - timedelta(days=days)).strftime('%Y%m%d'))

        # 获取后续表现
        training_data = []
        
        for _, pick in df_picks.iterrows():
            # 查询持有N天后的收益
            pct = self._get_performance(pick['ts_code'], pick['trade_date'])
            
            if pct is None:
                continue
                
            # 构建对话
            messages = [
                {
                    "role": "system",
                    "content": "你是一个专业的股票量化分析师，擅长选股和风险控制。"
                },
                {
                    "role": "user", 
                    "content": f"分析{pick['ts_code']} {pick['name']}，{pick['track']}策略得分{pick['final_score']:.2f}"
                },
                {
                    "role": "assistant",
                    "content": self._generate_response(pick, pct)
                }
            ]
            
            training_data.append({"messages": messages})

        return training_data

    def _get_performance(self, ts_code: str, buy_date: str, hold_days: int = 10) -> float:
        """获取持有N天后的收益"""
        try:
            df = DBUtils.query_df("""
                SELECT close FROM stock_daily 
                WHERE ts_code = %s AND trade_date = %s
            """, (ts_code, buy_date))
            
            if df.empty:
                return None
                
            buy_price = df.iloc[0]['close']
            
            # 持有后价格
            df2 = DBUtils.query_df("""
                SELECT close FROM stock_daily 
                WHERE ts_code = %s AND trade_date > %s
                ORDER BY trade_date
                LIMIT 1 OFFSET ?
            """, (ts_code, str(hold_days)))
            
            if df2.empty:
                return None
                
            sell_price = df2.iloc[0]['close']
            pct = (sell_price - buy_price) / buy_price * 100
            
            return pct
            
        except:
            return None

    def _generate_response(self, pick, pct: float) -> str:
        """生成回复"""
        result = "建议买入" if pct > 5 else ("不建议买入" if pct < 0 else "可以观望")
        
        return f"""分析：{pick['track']}策略得分{pick['final_score']:.2f}

持有{10}天后收益：{pct:.1f}%

结论：{result}

要点：
1. 策略得分{'高' if pick['final_score'] > 0.7 else '中' if pick['final_score'] > 0.5 else '低'}
2. 后续表现{'好' if pct > 5 else ('差' if pct < 0 else '一般')}
3. 建议：{'积极买入' if pct > 5 else ('谨慎' if pct < 0 else '观望')}"""

    def save(self, filename: str = None):
        """保存训练数据"""
        if filename is None:
            filename = f"finetune_{datetime.now().strftime('%Y%m%d')}.jsonl"
            
        data = self.build_training_data()
        
        with open(self.output_dir + filename, 'w', encoding='utf-8') as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
                
        logger.info(f"[Finetune] Saved {len(data)} records to {filename}")
        
        return len(data)


# 测试
if __name__ == "__main__":
    builder = FinetuneDataBuilder()
    
    print("=== Building finetune data ===")
    data = builder.build_training_data(days=30)
    print(f"Built {len(data)} records")
    
    if data:
        print("\nSample:")
        print(json.dumps(data[0], ensure_ascii=False, indent=2)[:300])
    
    print("=== Done ===")