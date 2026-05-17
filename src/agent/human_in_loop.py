"""
Human-in-loop - 买入前人工确认
通过DingTalk或Web API实现人工确认
"""
from typing import Optional, Dict
from loguru import logger
import time

from src.utils.notifier import DingTalkNotifier
from src.utils.config_loader import Config


class HumanInLoop:
    """人工确认机制"""

    def __init__(self):
        webhook = Config.get('notification.dingtalk.webhook')
        secret = Config.get('notification.dingtalk.secret_word', '提醒')
        self.notifier = DingTalkNotifier(webhook, secret_word=secret)
        self.pending_confirms = {}  # 存储待确认请求

    def request_buy_confirm(self, ts_code: str, name: str, price: float, 
                   reason: str, timeout: int = 300) -> Dict:
        """请求买入确认
        
        Args:
            ts_code: 股票代码
            name: 股票名称
            price: 买入价格
            reason: 买入理由
            timeout: 超时时间(秒)
            
        Returns:
            {'confirmed': True/False, 'confirmed_at': timestamp}
        """
        msg = f"""📈 买入确认请求

股票: {ts_code} {name}
价格: {price:.2f}
理由: {reason}

请在{timeout//60}分钟内回复"确认"或"取消"
        """
        
        # 发送确认请求
        self.notifier.send_message("买入确认", msg)
        
        # 记录待确认
        request_id = f"{ts_code}_{int(time.time())}"
        self.pending_confirms[request_id] = {
            'ts_code': ts_code,
            'name': name,
            'price': price,
            'reason': reason,
            'timestamp': time.time(),
            'timeout': timeout
        }
        
        return {
            'request_id': request_id,
            'status': 'pending',
            'message': '等待人工确认'
        }

    def check_confirm(self, request_id: str, user_response: str = None) -> bool:
        """检查确认状态
        
        Args:
            request_id: 请求ID
            user_response: 用户回复（通过Web/钉钉回调）
            
        Returns:
            True = 已确认, False = 未确认或超时
        """
        if request_id not in self.pending_confirms:
            return False
            
        req = self.pending_confirms[request_id]
        
        # 检查超时
        elapsed = time.time() - req['timestamp']
        if elapsed > req['timeout']:
            logger.warning(f"[HumanInLoop] 确认超时: {request_id}")
            del self.pending_confirms[request_id]
            return False
            
        # 检查用户回复（简化版，实际可通过钉钉回调获取）
        if user_response and '确认' in user_response:
            confirmed = True
        elif user_response and '取消' in user_response:
            confirmed = False
        else:
            return False  # 未确认
            
        del self.pending_confirms[request_id]
        return confirmed

    def need_human_approval(self, trade: Dict) -> bool:
        """判断是否需要人工审批
        
        根据规则判断：
        - 大额交易（>10万）需要确认
        - 新股票（不在持仓中）需要确认
        - 高风险策略需要确认
        """
        amount = trade.get('amount', 0)
        is_new = trade.get('is_new', True)
        
        # 大于10万或新股票需要确认
        if amount > 100000 or is_new:
            return True
            
        # 小额且非新股票可以自动执行
        return False


# 测试
if __name__ == "__main__":
    hil = HumanInLoop()
    
    print("=== Test HumanInLoop ===")
    
    # 测试需要确认的判断
    trade1 = {'amount': 150000, 'is_new': True}
    trade2 = {'amount': 50000, 'is_new': False}
    
    print(f"Trade1 (150k, new): need_confirm={hil.need_human_approval(trade1)}")
    print(f"Trade2 (50k, old): need_confirm={hil.need_human_approval(trade2)}")
    
    print("=== Done ===")