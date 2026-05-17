"""
TopicMapper: 从信息源标题/关键词映射到选股概念（hot_topics）

用于把「马斯克」「工信部」「发改委」等新闻关键词，
映射到 EventDriver 使用的概念名（特斯拉、芯片、新能源等）。
"""

from typing import List, Set
from src.utils.config_loader import Config


# 默认映射（当配置未提供时使用）
DEFAULT_TOPIC_MAPPING = {
    '马斯克': ['特斯拉'],
    '特斯拉': ['特斯拉'],
    '新能源': ['特斯拉', '新能源汽车'],
    '电动车': ['特斯拉'],
    '工信部': ['芯片', '人工智能', '工业互联网'],
    '发改委': ['新能源', '基建'],
    '证监会': ['券商', '金融'],
    '央行': ['黄金', '金融'],
    '半导体': ['芯片'],
    '芯片': ['芯片'],
    '人工智能': ['人工智能'],
    '机器人': ['机器人'],
    '低空': ['低空经济'],
    '黄金': ['黄金'],
}


class TopicMapper:
    """从文本中提取关键词并映射到选股概念"""

    def __init__(self, mapping: dict = None):
        """
        Args:
            mapping: 关键词 -> 概念列表。None 则从 Config('info_sources.topic_mapping') 或默认表读取。
        """
        if mapping is not None:
            self._mapping = {k: v if isinstance(v, list) else [v] for k, v in mapping.items()}
        else:
            raw = Config.get('info_sources.topic_mapping') or DEFAULT_TOPIC_MAPPING
            self._mapping = {k: v if isinstance(v, list) else [v] for k, v in raw.items()}

    def extract_topics_from_text(self, text: str) -> Set[str]:
        """
        从一段文本（如新闻标题）中提取匹配的选股概念。

        Args:
            text: 标题或正文片段

        Returns:
            匹配到的概念集合（去重）
        """
        if not text:
            return set()
        topics = set()
        for keyword, concepts in self._mapping.items():
            if keyword in text:
                topics.update(concepts)
        return topics

    def suggest_topics(self, texts: List[str]) -> List[str]:
        """
        从多段文本中汇总建议热点，按出现频率排序（简单按出现次数）。

        Args:
            texts: 多条标题或摘要

        Returns:
            建议的选股概念列表（去重，按出现次数降序）
        """
        from collections import Counter
        counter = Counter()
        for t in texts:
            for topic in self.extract_topics_from_text(t or ''):
                counter[topic] += 1
        return [k for k, _ in counter.most_common()]
