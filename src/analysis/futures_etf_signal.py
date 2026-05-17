#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于期货价格的ETF交易信号生成器
分析期货价格变化，生成有色、化工、贵金属板块ETF的买卖信号
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# 清除代理设置
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

from src.collector.futures_collector import FuturesCollector
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.log_utils import init_logger

logger = init_logger("futures_etf_signal")


class FuturesETFSignalGenerator:
    """基于期货价格的ETF交易信号生成器"""
    
    # ETF与期货的映射关系
    ETF_FUTURES_MAPPING = {
        # 有色金属ETF
        "有色金属": {
            "etf_keywords": ["有色", "金属", "铜", "铝", "锌", "铅", "镍", "锡"],
            "futures": ["铜", "铝", "锌", "铅", "镍", "锡"],
            "weights": {"铜": 0.3, "铝": 0.25, "锌": 0.15, "铅": 0.1, "镍": 0.15, "锡": 0.05}
        },
        # 贵金属ETF
        "贵金属": {
            "etf_keywords": ["黄金", "贵金属", "金", "银"],
            "futures": ["黄金", "白银"],
            "weights": {"黄金": 0.7, "白银": 0.3}
        },
        # 化工ETF
        "化工": {
            "etf_keywords": ["化工", "石化", "PTA", "甲醇", "PVC", "塑料", "PP", "橡胶"],
            "futures": ["原油", "PTA", "甲醇", "PVC", "塑料", "PP", "橡胶"],
            "weights": {"原油": 0.3, "PTA": 0.2, "甲醇": 0.15, "PVC": 0.1, "塑料": 0.1, "PP": 0.1, "橡胶": 0.05}
        }
    }
    
    def __init__(self):
        """初始化信号生成器"""
        self.futures_collector = FuturesCollector()
        
        # 从配置读取参数
        config = Config.get('futures_etf', {})
        self.buy_threshold = config.get('buy_threshold', 2.0)  # 期货涨幅超过2%触发买入
        self.sell_threshold = config.get('sell_threshold', -2.0)  # 期货跌幅超过2%触发卖出
        self.lookback_days = config.get('lookback_days', 5)  # 计算N天的价格变化
        self.signal_strength_threshold = config.get('signal_strength_threshold', 0.5)  # 信号强度阈值
        
        # 多时间框架配置
        self.short_term_days = config.get('short_term_days', 5)  # 短期：5天
        self.mid_term_days = config.get('mid_term_days', 20)  # 中期：20天
        self.long_term_days = config.get('long_term_days', 60)  # 长期：60天
        
        logger.info("期货ETF信号生成器初始化完成")
    
    def calculate_futures_score(self, sector: str) -> Tuple[float, Dict[str, float]]:
        """
        计算板块的综合期货得分（多时间框架）
        
        Args:
            sector: 板块名称（"有色金属"、"贵金属"、"化工"）
            
        Returns:
            (综合得分, 各期货品种得分字典)
        """
        if sector not in self.ETF_FUTURES_MAPPING:
            logger.warning(f"未知板块: {sector}")
            return 0.0, {}
        
        sector_config = self.ETF_FUTURES_MAPPING[sector]
        futures_list = sector_config['futures']
        weights = sector_config['weights']
        
        futures_scores = {}
        
        # 多时间框架得分
        short_term_scores = []
        mid_term_scores = []
        long_term_scores = []
        
        for futures_name in futures_list:
            # 计算不同时间框架的价格变化率
            short_change = self.futures_collector.get_futures_price_change(futures_name, days=self.short_term_days)
            mid_change = self.futures_collector.get_futures_price_change(futures_name, days=self.mid_term_days)
            long_change = self.futures_collector.get_futures_price_change(futures_name, days=self.long_term_days)
            
            if short_change is None and mid_change is None and long_change is None:
                logger.debug(f"无法获取{futures_name}价格变化")
                continue
            
            # 计算各时间框架得分
            short_score = max(-1.0, min(1.0, (short_change or 0) / 5.0)) if short_change is not None else 0
            mid_score = max(-1.0, min(1.0, (mid_change or 0) / 5.0)) if mid_change is not None else 0
            long_score = max(-1.0, min(1.0, (long_change or 0) / 5.0)) if long_change is not None else 0
            
            # 综合得分（加权平均：短期30%，中期50%，长期20%）
            combined_score = 0.3 * short_score + 0.5 * mid_score + 0.2 * long_score
            
            futures_scores[futures_name] = {
                'change_pct': short_change or mid_change or long_change or 0,
                'short_change': short_change,
                'mid_change': mid_change,
                'long_change': long_change,
                'score': combined_score,
                'short_score': short_score,
                'mid_score': mid_score,
                'long_score': long_score
            }
            
            # 收集各时间框架得分用于板块综合计算
            weight = weights.get(futures_name, 1.0 / len(futures_list))
            if short_change is not None:
                short_term_scores.append(short_score * weight)
            if mid_change is not None:
                mid_term_scores.append(mid_score * weight)
            if long_change is not None:
                long_term_scores.append(long_score * weight)
        
        # 计算板块综合得分（多时间框架加权）
        weighted_score = 0.0
        total_weight = 0.0
        
        if short_term_scores:
            weighted_score += 0.3 * sum(short_term_scores)
            total_weight += 0.3
        if mid_term_scores:
            weighted_score += 0.5 * sum(mid_term_scores)
            total_weight += 0.5
        if long_term_scores:
            weighted_score += 0.2 * sum(long_term_scores)
            total_weight += 0.2
        
        if total_weight > 0:
            weighted_score = weighted_score / total_weight
        
        return weighted_score, futures_scores
    
    def generate_signal(self, sector: str) -> Dict[str, any]:
        """
        生成ETF交易信号（增强版：包含持仓周期和操作建议）
        
        Args:
            sector: 板块名称
            
        Returns:
            信号字典：
            {
                'sector': 板块名称,
                'signal': 'BUY'/'SELL'/'HOLD',
                'strength': 信号强度(0-1),
                'score': 综合得分(-1到1),
                'futures_scores': 各期货品种得分,
                'reason': 信号原因,
                'holding_period': 建议持仓周期,
                'position_suggestion': 仓位建议,
                'operation_advice': 操作建议,
                'risk_level': 风险等级
            }
        """
        score, futures_scores = self.calculate_futures_score(sector)
        
        # 确定信号类型
        buy_threshold_norm = self.buy_threshold / 5.0
        sell_threshold_norm = self.sell_threshold / 5.0
        
        if score >= buy_threshold_norm:
            signal = 'BUY'
            strength = min(1.0, max(0.0, (score - buy_threshold_norm) / (1.0 - buy_threshold_norm)))
            reason = f"期货价格上涨，综合得分{score:.2f}，建议买入"
        elif score <= sell_threshold_norm:
            signal = 'SELL'
            strength = min(1.0, max(0.0, abs(score - sell_threshold_norm) / abs(-1.0 - sell_threshold_norm)))
            reason = f"期货价格下跌，综合得分{score:.2f}，建议卖出"
        else:
            signal = 'HOLD'
            strength = 0.5
            reason = f"期货价格波动较小，综合得分{score:.2f}，建议持有"
        
        # 根据信号强度确定持仓周期
        if signal == 'BUY':
            if strength > 0.8:
                holding_period = "1-2周"
                position_suggestion = "20-30%"
                risk_level = "中等"
            elif strength > 0.5:
                holding_period = "2-4周"
                position_suggestion = "10-20%"
                risk_level = "中等"
            else:
                holding_period = "4-8周"
                position_suggestion = "5-10%"
                risk_level = "较低"
        elif signal == 'SELL':
            holding_period = "立即执行"
            position_suggestion = "减仓或清仓"
            risk_level = "较高"
        else:
            holding_period = "观望"
            position_suggestion = "保持现有仓位"
            risk_level = "较低"
        
        # 生成操作建议
        operation_advice = self._generate_operation_advice(signal, strength, score, futures_scores)
        
        return {
            'sector': sector,
            'signal': signal,
            'strength': strength,
            'score': score,
            'futures_scores': futures_scores,
            'reason': reason,
            'holding_period': holding_period,
            'position_suggestion': position_suggestion,
            'operation_advice': operation_advice,
            'risk_level': risk_level,
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    
    def _generate_operation_advice(self, signal: str, strength: float, score: float, futures_scores: Dict) -> str:
        """
        生成详细操作建议
        
        Args:
            signal: 交易信号
            strength: 信号强度
            score: 综合得分
            futures_scores: 各期货品种得分
            
        Returns:
            操作建议字符串
        """
        advice_parts = []
        
        if signal == 'BUY':
            if strength > 0.8:
                advice_parts.append("【强信号】建议分批建仓，首次建仓50%，剩余50%在回调时加仓")
            elif strength > 0.5:
                advice_parts.append("【中等信号】建议分批建仓，首次建仓30%，观察后再决定是否加仓")
            else:
                advice_parts.append("【弱信号】建议小仓位试探，等待更明确的信号")
            
            # 分析主要驱动因素
            top_futures = sorted(futures_scores.items(), key=lambda x: x[1].get('score', 0), reverse=True)[:3]
            if top_futures:
                top_names = [name for name, _ in top_futures]
                advice_parts.append(f"主要驱动：{', '.join(top_names)}")
        elif signal == 'SELL':
            advice_parts.append("【卖出信号】建议逐步减仓，避免一次性清仓造成冲击")
            if strength > 0.7:
                advice_parts.append("信号较强，建议快速减仓至安全仓位")
        else:
            advice_parts.append("【持有信号】保持现有仓位，等待更明确的趋势信号")
            advice_parts.append("可适当关注期货价格变化，做好应对准备")
        
        return " | ".join(advice_parts)
    
    def match_etf_to_sector(self, etf_name: str) -> Optional[str]:
        """
        根据ETF名称匹配板块
        
        Args:
            etf_name: ETF名称
            
        Returns:
            板块名称，如果无法匹配返回None
        """
        etf_name_lower = etf_name.lower()
        
        for sector, config in self.ETF_FUTURES_MAPPING.items():
            keywords = config['etf_keywords']
            for keyword in keywords:
                if keyword in etf_name_lower:
                    return sector
        
        return None
    
    def generate_etf_signals(self, etf_list: List[Dict]) -> List[Dict]:
        """
        为ETF列表生成交易信号
        
        Args:
            etf_list: ETF列表，每个元素包含 'name', 'code' 等字段
            
        Returns:
            带信号的ETF列表
        """
        results = []
        
        # 按板块分组
        sector_etfs = {}
        for etf in etf_list:
            sector = self.match_etf_to_sector(etf.get('name', ''))
            if sector:
                if sector not in sector_etfs:
                    sector_etfs[sector] = []
                sector_etfs[sector].append(etf)
        
        # 为每个板块生成信号
        sector_signals = {}
        for sector in sector_etfs.keys():
            signal = self.generate_signal(sector)
            sector_signals[sector] = signal
        
        # 为每个ETF添加信号
        for etf in etf_list:
            sector = self.match_etf_to_sector(etf.get('name', ''))
            if sector and sector in sector_signals:
                signal_info = sector_signals[sector]
                etf['futures_signal'] = signal_info['signal']
                etf['futures_strength'] = signal_info['strength']
                etf['futures_score'] = signal_info['score']
                etf['futures_reason'] = signal_info['reason']
                etf['futures_sector'] = sector
            else:
                etf['futures_signal'] = 'N/A'
                etf['futures_strength'] = 0.0
                etf['futures_score'] = 0.0
                etf['futures_reason'] = '不适用期货信号'
                etf['futures_sector'] = None
            
            results.append(etf)
        
        return results
    
    def get_all_sector_signals(self) -> Dict[str, Dict]:
        """
        获取所有板块的信号
        
        Returns:
            字典：{板块名称: 信号字典}
        """
        signals = {}
        for sector in self.ETF_FUTURES_MAPPING.keys():
            signals[sector] = self.generate_signal(sector)
        return signals


if __name__ == '__main__':
    generator = FuturesETFSignalGenerator()
    
    # 获取所有板块信号
    print("=" * 60)
    print("期货ETF交易信号")
    print("=" * 60)
    
    signals = generator.get_all_sector_signals()
    for sector, signal_info in signals.items():
        print(f"\n【{sector}】")
        print(f"  信号: {signal_info['signal']}")
        print(f"  强度: {signal_info['strength']:.2f}")
        print(f"  得分: {signal_info['score']:.2f}")
        print(f"  原因: {signal_info['reason']}")
        print(f"  期货详情:")
        for futures_name, futures_data in signal_info['futures_scores'].items():
            print(f"    {futures_name}: {futures_data['change_pct']:+.2f}% (得分: {futures_data['score']:.2f})")
