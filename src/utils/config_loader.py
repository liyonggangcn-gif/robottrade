import os
import re
import yaml
from typing import Dict, Any

class ConfigLoader:
    """配置文件加载器"""

    _instance = None
    _config = None

    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            cls._instance = super(ConfigLoader, cls).__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """递归合并 override 到 base（override 优先）"""
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    def _resolve_env_vars(self, obj):
        """递归解析 ${VAR_NAME} 占位符，优先从环境变量读取"""
        if isinstance(obj, dict):
            return {k: self._resolve_env_vars(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve_env_vars(i) for i in obj]
        if isinstance(obj, str):
            def replacer(m):
                var = m.group(1)
                return os.environ.get(var, m.group(0))  # 未设置时保留占位符原文
            return re.sub(r'\$\{([^}]+)\}', replacer, obj)
        return obj

    def _load_config(self):
        """加载配置文件"""
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'settings.yaml')
        self._config_path = os.path.abspath(config_path)
        self._project_root = os.path.dirname(os.path.dirname(self._config_path))

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
            print(f"Successfully loaded config from {config_path}")
        except Exception as e:
            print(f"Failed to load config: {e}")
            raise

        # 加载本地 secrets 覆盖文件（不纳入 git，优先级高于 settings.yaml）
        secrets_path = os.path.join(os.path.dirname(self._config_path), 'secrets.local.yaml')
        if os.path.exists(secrets_path):
            try:
                with open(secrets_path, 'r', encoding='utf-8') as f:
                    secrets = yaml.safe_load(f) or {}
                self._deep_merge(self._config, secrets)
            except Exception as e:
                print(f"[Config] Warning: failed to load secrets.local.yaml: {e}")

        # 解析 ${VAR_NAME} 占位符（环境变量优先）
        self._config = self._resolve_env_vars(self._config)

        self._validate_config()

    def _validate_config(self):
        """校验必填/推荐配置项，缺失时打印明确警告"""
        warnings = []

        # 必须有 tushare_token
        token = self._config.get('tushare_token', '')
        if not token or 'YOUR_' in str(token):
            warnings.append("❌ tushare_token 未配置，数据同步将失败")

        # db_type 默认 sqlite，但提示
        db_type = self._config.get('db_type', 'sqlite')
        if db_type not in ('sqlite', 'mysql'):
            warnings.append(f"⚠️  db_type='{db_type}' 非法，请设为 'sqlite' 或 'mysql'，已降级为 sqlite")
            self._config['db_type'] = 'sqlite'

        # notification webhook（非必须，但推荐配置）
        webhook = (self._config.get('notification') or {}).get('dingtalk', {}).get('webhook', '')
        if not webhook:
            warnings.append("⚠️  notification.dingtalk.webhook 未配置，钉钉推送将失败")

        # LLM API key（非必须，功能降级）
        llm_key = (self._config.get('llm') or {}).get('api_key', '')
        if not llm_key or 'YOUR_' in str(llm_key):
            warnings.append("⚠️  llm.api_key 未配置，LLM功能将禁用")

        if warnings:
            print("[Config] 配置校验警告：")
            for w in warnings:
                print(f"  {w}")
    
    @property
    def project_root(self) -> str:
        """项目根目录（绝对路径），用于解析 data/ 等相对路径，避免计划任务 cwd 异常"""
        return getattr(self, '_project_root', os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        keys = key.split('.')
        value = self._config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    @property
    def tushare_token(self) -> str:
        """获取Tushare Token"""
        return self.get('tushare_token', '')
    
    @property
    def duckdb_path(self) -> str:
        """获取数据库路径（兼容 duckdb_path/db_path）"""
        return self.get('db_path') or self.get('duckdb_path', 'data/quant.db')
    
    @property
    def start_date(self) -> str:
        """获取回测起始日期"""
        return self.get('start_date', '20200101')
    
    def __getattr__(self, name: str) -> Any:
        """支持通过属性访问配置"""
        return self.get(name)

# 创建全局配置实例
Config = ConfigLoader()

if __name__ == '__main__':
    # 测试代码
    print(f"Tushare Token: {Config.tushare_token}")
    print(f"DuckDB Path: {Config.duckdb_path}")
    print(f"Start Date: {Config.start_date}")
    print(f"Strategy TopK: {Config.get('strategy.topk')}")
