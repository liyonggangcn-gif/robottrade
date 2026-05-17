import requests
import json
import time
from loguru import logger

# 推送重试配置
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2  # 秒，指数退避基值


class BaseNotifier:
    """消息推送基类"""
    
    def send_message(self, title, content):
        """发送消息
        
        Args:
            title: 消息标题
            content: 消息内容
            
        Returns:
            bool: 是否发送成功
        """
        raise NotImplementedError

class DingTalkNotifier(BaseNotifier):
    """钉钉消息推送"""
    
    def __init__(self, webhook_url, secret_word=None, log_message=True):
        """初始化钉钉推送
        
        Args:
            webhook_url: 钉钉机器人webhook地址
            secret_word: 安全关键词（必须包含在标题或内容中）
            log_message: 是否记录消息到数据库
        """
        self.webhook_url = webhook_url
        self.secret_word = secret_word
        self.log_message = log_message
    
    @staticmethod
    def _optimize_content_for_mobile(content: str) -> str:
        """优化内容适配钉钉手机端 Markdown 渲染

        钉钉 Markdown 注意事项：
        - 支持 - item 列表，不要替换为 •（会破坏渲染）
        - 支持 ``` 代码块，不要替换为单反引号
        - 表格完全支持 | col | 语法
        - 单行超过约 200 字时截断避免溢出
        """
        lines = content.split('\n')
        result = []
        for line in lines:
            # 非表格行、非代码行超过 200 字时尾部截断
            if len(line) > 200 and not line.startswith('|') and not line.startswith('```'):
                line = line[:197] + '...'
            result.append(line)
        return '\n'.join(result)
    
    def send_message(self, title, content, max_retries=DEFAULT_MAX_RETRIES, message_type='unknown'):
        """发送消息到钉钉（支持重试，指数退避）
        
        Args:
            title: 消息标题
            content: 消息内容
            max_retries: 最大重试次数（含首次）
            
        Returns:
            bool: 是否发送成功
        """
        headers = {'Content-Type': 'application/json'}
        if self.secret_word and self.secret_word not in title:
            title = f"{title}（{self.secret_word}）"
        
        # 优化手机端显示：添加响应式格式
        optimized_content = self._optimize_content_for_mobile(content)
        
        data = {
            'msgtype': 'markdown',
            'markdown': {'title': title, 'text': f"## {title}\n\n{optimized_content}"}
        }
        
        last_err = None
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    delay = DEFAULT_RETRY_DELAY * (2 ** (attempt - 1))
                    logger.info(f"DingTalk 重试 {attempt}/{max_retries-1}，{delay}s 后...")
                    time.sleep(delay)
                logger.info(f"Sending message to DingTalk: {title}")
                response = requests.post(
                    self.webhook_url,
                    headers=headers,
                    data=json.dumps(data),
                    timeout=10
                )
                result = response.json()
                if result.get('errcode') == 0:
                    logger.success("Message sent successfully to DingTalk")
                    # 记录消息到数据库
                    if self.log_message:
                        try:
                            from src.utils.message_logger import MessageLogger
                            logger_instance = MessageLogger()
                            logger_instance.log_message(
                                message_type=message_type,
                                title=title,
                                content=content,
                                send_status='success'
                            )
                        except Exception as e:
                            logger.warning(f"记录消息失败: {e}")
                    return True
                last_err = result.get('errmsg', 'unknown')
                logger.warning(f"DingTalk 返回错误: {last_err}")
            except Exception as e:
                last_err = e
                logger.warning(f"DingTalk 请求异常: {e}", exc_info=True)
        
        logger.error(f"钉钉推送失败（已重试 {max_retries} 次）: {last_err}")
        # 记录失败消息
        if self.log_message:
            try:
                from src.utils.message_logger import MessageLogger
                logger_instance = MessageLogger()
                logger_instance.log_message(
                    message_type=message_type,
                    title=title,
                    content=content,
                    send_status='failed',
                    error_message=str(last_err)
                )
            except Exception as e:
                logger.warning(f"记录失败消息失败: {e}")
        return False

class PushPlusNotifier(BaseNotifier):
    """PushPlus消息推送（支持微信）"""
    
    def __init__(self, token):
        """初始化PushPlus推送
        
        Args:
            token: PushPlus的token
        """
        self.token = token
        self.api_url = "http://www.pushplus.plus/send"
    
    def send_message(self, title, content):
        """发送消息到PushPlus
        
        Args:
            title: 消息标题
            content: 消息内容
            
        Returns:
            bool: 是否发送成功
        """
        try:
            data = {
                'token': self.token,
                'title': title,
                'content': content,
                'template': 'markdown'
            }
            
            logger.info(f"Sending message to PushPlus: {title}")
            response = requests.post(
                self.api_url,
                data=data,
                timeout=10
            )
            
            result = response.json()
            if result.get('code') == 200:
                logger.success("Message sent successfully to PushPlus")
                return True
            else:
                logger.error(f"Failed to send message to PushPlus: {result.get('msg')}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending message to PushPlus: {e}")
            return False

class FeishuNotifier(BaseNotifier):
    """飞书消息推送"""
    
    def __init__(self, webhook_url, secret_word=None):
        """初始化飞书推送
        
        Args:
            webhook_url: 飞书机器人webhook地址
            secret_word: 关键词（必须包含在标题或内容中）
        """
        self.webhook_url = webhook_url
        self.secret_word = secret_word
    
    def send_message(self, title, content):
        """发送消息到飞书
        
        Args:
            title: 消息标题
            content: 消息内容
            
        Returns:
            bool: 是否发送成功
        """
        try:
            headers = {
                'Content-Type': 'application/json'
            }
            
            # 构建消息体（支持消息卡片）
            data = {
                "msg_type": "interactive",
                "card": {
                    "config": {
                        "wide_screen_mode": True,
                        "enable_forward": True
                    },
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"{title}（{self.secret_word if self.secret_word else ''}）"
                        },
                        "template": "blue"
                    },
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": content
                        }
                    ]
                }
            }
            
            logger.info(f"Sending message to Feishu: {title}")
            response = requests.post(
                self.webhook_url,
                headers=headers,
                data=json.dumps(data),
                timeout=10
            )
            
            result = response.json()
            if result.get('code') == 0:
                logger.success("Message sent successfully to Feishu")
                return True
            else:
                logger.error(f"Failed to send message to Feishu: {result.get('msg')}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending message to Feishu: {e}")
            return False

class NotifierFactory:
    """消息推送工厂类"""
    
    @staticmethod
    def create_notifier(notifier_type, **kwargs):
        """创建消息推送实例
        
        Args:
            notifier_type: 推送类型，可选值：'dingtalk', 'pushplus', 'feishu'
            **kwargs: 推送所需的参数
            
        Returns:
            BaseNotifier: 消息推送实例
        """
        if notifier_type == 'dingtalk':
            return DingTalkNotifier(
                webhook_url=kwargs.get('webhook_url'),
                secret_word=kwargs.get('secret_word')
            )
        elif notifier_type == 'pushplus':
            return PushPlusNotifier(
                token=kwargs.get('token')
            )
        elif notifier_type == 'feishu':
            return FeishuNotifier(
                webhook_url=kwargs.get('webhook_url'),
                secret_word=kwargs.get('secret_word')
            )
        else:
            raise ValueError(f"Unsupported notifier type: {notifier_type}")

def build_dingtalk_card(title: str, content: str, message_type: str = "alert") -> str:
    """
    根据消息类型生成结构化的钉钉 Markdown 正文。
    不同类型有各自的颜色标识和页眉分隔线，提升可读性。

    Args:
        title:        消息标题（已含 emoji）
        content:      消息正文（Markdown，调用者负责结构化）
        message_type: 消息类型标签

    Returns:
        str: 完整的钉钉 Markdown text 字段内容
    """
    # 类型 → 页眉装饰（emoji + 颜色标签行）
    TYPE_META = {
        'agent_decision': ('📋', '**【Agent 盘前决策】**'),
        'agent_risk':     ('⚠️', '**【风控预警】**'),
        'agent_trade':    ('✅', '**【交易成交确认】**'),
        'morning':        ('🌅', '**【早盘推荐】**'),
        'evening':        ('🌆', '**【晚间复盘】**'),
        'strategy':       ('🔍', '**【策略选股】**'),
        'etf':            ('📊', '**【ETF信号】**'),
        'sync':           ('🔄', '**【数据同步】**'),
        'alert':          ('🔔', '**【系统通知】**'),
    }
    icon, tag = TYPE_META.get(message_type, ('📌', f'**【{message_type}】**'))
    now_str = __import__('datetime').datetime.now().strftime('%m-%d %H:%M')

    # 拼装：类型标签 + 分割线 + 正文
    header = f"{tag}  <font color=#999999>{now_str}</font>\n\n---\n\n"
    return header + content


def send_alert(title: str, content: str, message_type: str = "alert") -> bool:
    """
    从配置文件读取钉钉 webhook，直接推送一条即时消息。
    供各模块调用，无需关心 webhook 配置细节。

    Args:
        title:        消息标题
        content:      消息正文（Markdown）
        message_type: 类型标签，用于日志区分（如 buy_signal / stop_loss / analysis）

    Returns:
        bool: True=推送成功
    """
    try:
        from src.utils.config_loader import Config
        notification_config = Config.get('notification') or {}
        if not notification_config.get('enabled', False):
            return False
        if notification_config.get('provider', 'dingtalk') != 'dingtalk':
            return False
        dingtalk_cfg = notification_config.get('dingtalk') or {}
        webhook_url = dingtalk_cfg.get('webhook')
        secret_word = dingtalk_cfg.get('secret_word', '提醒')
        if not webhook_url:
            return False
        notifier = DingTalkNotifier(webhook_url=webhook_url, secret_word=secret_word)
        # 用卡片格式包装正文，提升专业性
        formatted_content = build_dingtalk_card(title, content, message_type)
        return notifier.send_message(title, formatted_content, message_type=message_type)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"send_alert 失败: {e}")
        return False


if __name__ == '__main__':
    # 测试代码
    # 测试钉钉推送
    # dingtalk_notifier = DingTalkNotifier(
    #     webhook_url='https://oapi.dingtalk.com/robot/send?access_token=your_token'
    # )
    # dingtalk_notifier.send_message('测试消息', '这是一条测试消息')
    
    # 测试PushPlus推送
    # pushplus_notifier = PushPlusNotifier(
    #     token='your_pushplus_token'
    # )
    # pushplus_notifier.send_message('测试消息', '这是一条测试消息')
    pass
