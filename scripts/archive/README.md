# 归档脚本

本目录存放已废弃或仅用于调试的脚本，日常使用请勿调用。

## 主要入口（scripts/ 根目录）

| 脚本 | 用途 |
|------|------|
| daily_alpha_run.py | 每日完整流水线（同步+选股+钉钉） |
| morning_push.py | 早盘推送（8:30） |
| evening_push.py | 收盘推送（16:00） |
| push_etf_dingtalk.py | ETF 策略推送到钉钉 |
| start_all_services.py | 数据同步+选股+Web 界面 |
| run_ai_model.py | LightGBM AI 训练 |
| sync_tushare_data.py | Tushare 数据同步 |

## 归档说明

- `_*` 前缀脚本：调试/验证用，已归档
- 迁移后若有引用请更新路径
