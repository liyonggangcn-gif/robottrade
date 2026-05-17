# QuantAgent-Alpha 系统架构文档

## 项目概述

**QuantAgent-Alpha** — 混合AI驱动的量化股票选股系统，专为中国A股市场设计。

- **定位**: 量化选股 + AI决策 + 自动交易
- **数据源**: Tushare / eFinance / Baostock / AKShare
- **数据库**: MySQL + ClickHouse
- **部署**: 192.168.3.22 (Linux) + Windows本地开发

---

## 系统架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                         定时任务调度 (Cron)                         │
│   07:30 AI训练  08:00 选股  08:30 AI决策  08:35 早盘推送            │
│   16:00 收盘推送  17:00 数据同步  17:30 数据稽核                     │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        核心流水线 (daily_alpha_run.py)             │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │ Step 0  │  │ Step 1  │  │ Step 2  │  │ Step 3  │  │ Step 4  │ │
│  │新闻分析│→│数据同步 │→│概念同步 │→│AI模型  │→│混合选股 │ │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────┘ │
│       │                                              │            │
│       │                                              ▼            │
│       │                                    ┌─────────────────┐    │
│       │                                    │  Step 4e 持仓管理 │    │
│       │                                    └─────────────────┘    │
│       │                                              │            │
│       ▼                                              ▼            │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                    Step 5: 自动交易                      │    │
│  └──────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 模块结构

### 1. 数据层 (src/collector)

| 模块 | 功能 |
|------|------|
| `data_loader.py` | 主数据加载器，支持多源(Tushare/eFinance/Baostock) |
| `enhanced_data_loader.py` | 增强数据加载(北向资金/龙虎榜/舆情) |
| `ch_data_loader.py` | ClickHouse数据加载器 |
| `multi_source_adapter.py` | 多数据源适配器 |
| `data_quality.py` | 数据质量检查 |
| `gov_news_fetcher.py` | 官媒新闻抓取 |

### 2. 因子层 (src/factors)

| 模块 | 功能 |
|------|------|
| `alpha_engine.py` | 计算34个技术因子(RSI/MACD/布林带等) |

### 3. 策略层 (src/strategy)

| 策略 | 描述 | 权重 |
|------|------|------|
| `hybrid_strategy.py` | AI混合策略(50% AI + 30% 事件 + 20% 基本面) | 40% |
| `value_strategy.py` | 价值投资策略(PE/ROE/FCF) | 20% |
| `dividend_strategy.py` | 高股息策略 | 15% |
| `small_cap_strategy.py` | 小市值策略 | 15% |
| `momentum_short.py` | 短线动量策略 | 10% |
| `cyclical_strategy.py` | 周期轮动策略 | 20% |
| `pb_roa_strategy.py` | PB-ROA价值策略 | 20% |
| `convertible_bond_strategy.py` | 可转债策略 | 10% |
| `index_enhance_strategy.py` | 指数增强策略 | 15% |
| `etf_bottom_fish_strategy.py` | ETF抄底反弹策略 | - |
| `pool_strategy.py` | 股票池策略 | - |
| `center.py` | 策略中心(多策略并行) | - |

### 4. 分析层 (src/analysis)

| 模块 | 功能 |
|------|------|
| `event_driver.py` | 事件驱动分析(热点概念映射) |
| `industry_timing.py` | 行业轮动(渗透率+经济周期) |
| `hot_topic_detector.py` | 动态热点识别 |
| `stock_analyzer.py` | 个股分析 |
| `financial_fetcher.py` | 财务数据获取 |
| `etf_selector.py` | ETF选择器 |

### 5. LLM层 (src/llm)

| 模块 | 功能 |
|------|------|
| `research_reasoner.py` | 个股深度研究(ResearchReasoner) |
| `selection_reviewer.py` | 选股审核(SelectionReviewer) |
| `daily_reporter.py` | 日报生成(DailyReporter) |
| `base.py` | LLM基类 |

### 6. Agent层 (src/agent)

| 模块 | 功能 |
|------|------|
| `trading_agent.py` | 交易Agent主编排器 |
| `decision_engine.py` | 决策引擎 |
| `risk_controller.py` | 风控控制器 |
| `review_agent.py` | 复盘Agent |
| `trade_memory.py` | 交易记忆存储 |
| `multi_agent/` | 多Agent系统(orchestrator/strategy_agent/risk_agent/execution_agent) |

### 7. 交易层 (src/trading & src/broker)

| 模块 | 功能 |
|------|------|
| `auto_trader.py` | 自动交易执行 |
| `sim_broker.py` | 模拟券商(回测) |
| `xt_broker.py` | 迅投实盘券商 |
| `iquant_broker.py` | 聚宽实盘券商 |
| `base_broker.py` | 券商基类 |

### 8. 持仓层 (src/portfolio)

| 模块 | 功能 |
|------|------|
| `position_manager.py` | 持仓管理(止损/止盈/风控) |
| `holding_manager.py` | 持仓稳定性管理 |
| `portfolio_manager.py` | 组合管理器 |
| `recommendation_tracker.py` | 推荐追踪 |

### 9. 风控层 (src/risk)

| 模块 | 功能 |
|------|------|
| `market_news_analyzer.py` | 市场新闻分析(LLM风险评估) |
| `event_risk_monitor.py` | 事件风控监控 |

### 10. Web层 (src/web)

| 模块 | 功能 |
|------|------|
| `api.py` | REST API(选股/策略/持仓/Agent) |
| `app.py` | FastAPI应用 |

### 11. 数据可视化 (src/app)

| 模块 | 功能 |
|------|------|
| `dashboard.py` | Streamlit仪表盘(交易驾驶舱) |
| `pages/portfolio.py` | 持仓页面 |
| `pages/strategy_center.py` | 选股中心页面 |

### 12. 回测层 (src/backtest)

| 模块 | 功能 |
|------|------|
| `backtest_engine.py` | 回测引擎 |
| `historical_backtester.py` | 历史回测 |
| `performance_tracker.py` | 绩效追踪 |
| `tables.py` | 回测结果表 |

---

## 数据流程

```
┌──────────────────┐
│  Tushare/eFinance │ → 数据同步 → MySQL/ClickHouse
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  alpha_engine    │ → 计算34个技术因子
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  run_ai_model.py │ → LightGBM训练 → ai_predictions表
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  HybridStrategy │ → 混合评分: 50%AI + 30%事件 + 20%基本面
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Agent决策      │ → 生成交易信号
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  自动交易       │ → sim/xtquant/iquant broker
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  钉钉推送       │ → 选股结果/日报/告警
└──────────────────┘
```

---

## 核心配置 (config/settings.yaml)

```yaml
# 数据源
tushare_token: "xxx"
db_type: "mysql"
mysql:
  host: "192.168.3.41"
  port: 3306
clickhouse:
  enable: true
  host: "192.168.3.51"

# 选股参数
strategy:
  topk: 20
  hybrid:
    ai_weight: 0.5
    event_weight: 0.3
    fund_weight: 0.2

# 推送配置
notification:
  dingtalk:
    webhook: "xxx"
    secret_word: "提醒"

# 风控
risk:
  stop_loss: -0.08
  circuit_breaker: -0.03
```

---

## 定时任务 (见 CRON_SCHEDULE.md)

| 时间 | 任务 |
|------|------|
| 07:30 | run_ai_model.py - AI模型训练 |
| 08:00 | daily_alpha_run.py - 数据同步+选股 |
| 08:30 | run_trading_agent.py - AI决策 |
| 08:35 | morning_push.py - 早盘推送 |
| 16:00 | evening_push.py - 收盘推送 |
| 17:00 | sync_enhanced_data.py - 增强数据同步 |
| 17:30 | check_data_quality.py - 数据质量稽核 |
| 每30分钟 | fetch_news.py - 财经新闻 |

---

## 输出文件

- `output/hybrid_picks_YYYYMMDD.csv` - 混合策略选股结果
- `output/multi_strategy_YYYYMMDD.json` - 多策略预跑缓存
- `output/etf_picks_YYYYMMDD.csv` - ETF选股结果
- `logs/daily_alpha_*.log` - 每日流水线日志

---

## 依赖

```
pandas
numpy
lightgbm
tushare
akshare
efinance
baostock
clickhouse-connect
pymysql
streamlit
fastapi
loguru
```

---

## 部署架构

```
┌─────────────────┐      ┌─────────────────┐
│   Windows开发   │ ──── │  192.168.3.22   │
│  DESKTOP-FREP7AV│  Git │   Linux服务器   │
└─────────────────┘      └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌─────────┐  ┌─────────┐  ┌─────────┐
              │  MySQL  │  │ClickHouse│  │ 钉钉推送 │
              │:3306   │  │ :8123   │  │         │
              └─────────┘  └─────────┘  └─────────┘
```

---

## 目录结构

```
robottrade/
├── config/
│   └── settings.yaml          # 配置文件
├── scripts/                   # 执行脚本
│   ├── daily_alpha_run.py    # 主流水线
│   ├── morning_push.py       # 早盘推送
│   ├── evening_push.py       # 收盘推送
│   ├── run_ai_model.py       # AI训练
│   ├── run_trading_agent.py # Agent决策
│   └── ...
├── src/
│   ├── collector/            # 数据采集
│   ├── factors/              # 因子计算
│   ├── strategy/             # 选股策略
│   ├── analysis/             # 分析模块
│   ├── llm/                 # LLM调用
│   ├── agent/               # Agent系统
│   ├── broker/              # 券商接口
│   ├── portfolio/            # 持仓管理
│   ├── risk/                # 风控
│   ├── backtest/            # 回测
│   ├── web/                 # API
│   └── app/                 # Dashboard
├── output/                   # 输出结果
└── logs/                    # 日志文件
```