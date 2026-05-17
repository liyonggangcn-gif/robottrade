"""
向量Memory模块 - 轻量级语义搜索
使用DeepSeek API做嵌入，本地SQLite存储
"""
import json
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import requests
from loguru import logger

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class VectorMemory:
    """向量记忆系统 - 轻量级实现"""

    TABLE_NAME = "agent_vector_memory"

    def __init__(self):
        self._ensure_table()
        self._embedding_cache = {}

    def _ensure_table(self):
        """建表"""
        DBUtils.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_type VARCHAR(20),
                title VARCHAR(200),
                content TEXT,
                embedding JSON,
                ts_code VARCHAR(20),
                trade_date VARCHAR(10),
                importance INT DEFAULT 1,
                created_at VARCHAR(20)
            )
        """)

    def _get_embedding(self, text: str) -> List[float]:
        """获取嵌入向量：优先API，失败则用本地hash模拟"""
        if text in self._embedding_cache:
            return self._embedding_cache[text]

        # 尝试DeepSeek API
        embedding = self._get_embedding_api(text)
        if embedding != [0.0] * len(embedding):
            return embedding

        # 回退到本地hash模拟（用于测试）
        return self._local_embedding(text)

    def _get_embedding_api(self, text: str) -> List[float]:
        """调用API获取嵌入"""
        try:
            api_key = Config.get('deepseek_api_key')
            if not api_key:
                api_key = Config.get('llm.api_key')
            if not api_key:
                api_key = Config.get('llm_router.analyzer.api_key')

            if not api_key:
                return [0.0] * 1024

            # 尝试DeepSeek v1/embeddings
            for url, model in [
                ("https://api.deepseek.com/v1/embeddings", "deepseek-embedding"),
                ("https://api.deepseek.com/chat/completions", "deepseek-chat"),
            ]:
                try:
                    resp = requests.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={"input": text, "model": model} if "embed" in url else {
                            "messages": [{"role": "user", "content": f"Generate embedding for: {text}"}],
                            "model": model
                        },
                        timeout=30
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if 'data' in data:
                            embedding = data['data'][0].get('embedding', [0.0] * 1024)
                            self._embedding_cache[text] = embedding
                            return embedding
                except:
                    pass

            return [0.0] * 1024

        except Exception as e:
            logger.warning(f"[VectorMemory] API error: {e}")
            return [0.0] * 1024

    def _local_embedding(self, text: str) -> List[float]:
        """本地hash模拟嵌入（用于无API时）"""
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        vec = [float(b) / 255.0 for b in h]
        # 扩展到1024维
        while len(vec) < 1024:
            vec = vec + vec[:1024-len(vec)]
        return vec[:1024]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """计算余弦相似度"""
        if not a or not b:
            return 0.0
        a = np.array(a)
        b = np.array(b)
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))

    def save(self, memory_type: str, title: str, content: str,
           ts_code: str = '', trade_date: str = '', importance: int = 1):
        """保存记忆（自动生成嵌入）"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        text = f"{title} {content}"
        embedding = self._get_embedding(text)

        try:
            DBUtils.execute(f"""
                INSERT INTO {self.TABLE_NAME}
                (memory_type, title, content, embedding, ts_code, trade_date, importance, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (memory_type, title, content, json.dumps(embedding), ts_code, trade_date, importance, now))
            logger.info(f"[VectorMemory] Saved: {title}")
        except Exception as e:
            logger.error(f"[VectorMemory] Save failed: {e}")

    def search(self, query: str, top_k: int = 5, days: int = 60) -> List[Dict]:
        """语义搜索"""
        query_embedding = self._get_embedding(query)
        
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        df = DBUtils.query_df(f"""
            SELECT id, memory_type, title, content, embedding, ts_code, importance
            FROM {self.TABLE_NAME}
            WHERE created_at >= ?
            ORDER BY importance DESC
            LIMIT 50
        """, (cutoff,))

        if df.empty:
            return []

        results = []
        for _, row in df.iterrows():
            emb = json.loads(row['embedding']) if isinstance(row['embedding'], str) else row['embedding']
            sim = self._cosine_similarity(query_embedding, emb)
            results.append({
                'id': row['id'],
                'memory_type': row['memory_type'],
                'title': row['title'],
                'content': row['content'],
                'ts_code': row.get('ts_code', ''),
                'importance': row['importance'],
                'similarity': sim
            })

        results.sort(key=lambda x: x['similarity'], reverse=True)
        return results[:top_k]

    def get_context_prompt(self, query: str = '', top_k: int = 5) -> str:
        """生成记忆上下文字符串"""
        if query:
            results = self.search(query, top_k=top_k)
        else:
            results = self.search("交易 选股 经验", top_k=top_k)

        if not results:
            return ""

        lines = ["【相关记忆】"]
        for r in results:
            score = r.get('similarity', 0)
            title = r.get('title', '')
            content = r.get('content', '')[:80]
            lines.append(f"- {title}: {content} [相似度={score:.2f}]")

        return "\n".join(lines)


# 测试
if __name__ == "__main__":
    vm = VectorMemory()
    
    print("=== 测试保存 ===")
    vm.save("win_pattern", "小市值策略成功", "2026年4月选出10只，2周后平均收益5%", importance=3)
    
    print("=== 测试搜索 ===")
    results = vm.search("选股策略")
    for r in results:
        print(f"  {r['title']}: similarity={r['similarity']:.3f}")
    
    print("=== 测试上下文 ===")
    ctx = vm.get_context_prompt("如何选股")
    print(ctx)