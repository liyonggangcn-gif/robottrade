# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**QuantAgent-Alpha** — A hybrid AI-driven quantitative stock selection system for Chinese A-share markets, combining:
- LightGBM AI predictions (50% weight) based on 34 technical indicators
- Event-driven concept matching (30% weight) from hot topics + RSS feeds
- Fundamental analysis (20% weight) using ROE, PE, and other financial metrics

Outputs Top 20 stock picks daily to `output/hybrid_picks_YYYYMMDD.csv` and sends DingTalk notifications.

## Selection Center (选股中心)

**12 strategies available** via `src/strategy/center.py` (StrategyCenter):
- `hybrid` — AI混合策略 (权重 40%)
- `sector_rotation` — 行业轮动策略 (30%)
- `value` — 价值投资策略 (20%)
- `dividend` — 红利策略 (15%)
- `quant` — 量化多因子策略 (15%)
- `small_cap` — 质量小市值策略 (15%)
- `small_cap_pure` — 纯小市值策略 (10%)
- `small_cap_jinx` — 小市值Jinx择时 (10%)
- `cyclical` — 周期轮动策略 (20%)
- `pb_roa` — PB-ROA价值策略 (20%)
- `convertible_bond` — 可转债策略 (10%)
- `index_enhance` — 指数增强策略 (15%)

**API endpoints** (`src/web/api.py`):
- `GET /api/strategies/available` — 列出所有可用策略
- `POST /api/strategies/run` — 多策略并行选股（StrategyCenter）
- `GET /api/strategies/results` — 读取预跑缓存（30分钟TTL）
- `POST /api/strategies/run_cached` — 优先返回缓存，否则重新计算
- `GET /api/strategies/memory_facts` — 获取记忆事实用于选股加成
- `GET /api/memory/summary` — 记忆摘要

**记忆加成**: MemoryService 高置信度事实 → HybridStrategy Step 7f 应用 +0.05~0.03 加成

**预跑缓存**: `output/multi_strategy_YYYYMMDD.json`，由 `daily_alpha_run.py` 自动生成（8:00 cron）

## Web Navigation (选股中心整合后)

**核心页面**：`/selection` — 选股中心（整合了以下所有功能）

选股中心标签页：
- 选股结果 — 双轨混合选股结果，支持股票/ETF/转债过滤
- 市场研究 — 子标签：个股分析 + 新闻舆情 + ETF策略 + 可转债 + 策略回测 + 板块管理
- **股票池** — 子标签：买入信号 + 名单管理 + 健康检查 + 估值一览
- **LLM决策** — 子标签：决策历史 + 交易决策 + Agent预盘
- 历史选股 — 执行历史记录
- 策略管理 — 12个策略卡片动态加载

**已合并的路由**（自动重定向到 `/selection`）：
- `/pool` → `/selection` (tab=pool)
- `/etf` → `/selection` (tab=etf)
- `/cb` → `/selection` (tab=cb)
- `/backtest` → `/selection` (tab=backtest)
- `/strategy` → `/selection`

**LLM决策 API**：
- `GET /api/llm/evaluations` — LLM推理历史（ResearchReasoner/SelectionReviewer/DailyReporter）
- `GET /api/llm/evaluation/latest` — 最新LLM决策详情
- `GET /api/llm/trader_decisions` — LLM交易决策（含人工确认状态）
- `GET /api/agent/decisions` — Agent预盘决策历史

## Commands

### Daily Operations
```bash
# Full pipeline: data sync + AI training + strategy execution
python scripts/daily_alpha_run.py

# Skip AI retraining (use existing scores)
python scripts/daily_alpha_run.py --skip-qlib

# Stock selection only (no sync, no AI retraining)
python scripts/daily_alpha_run.py --skip-sync --skip-qlib

# Start Streamlit dashboard
streamlit run src/app/dashboard.py --server.port 8502

# Individual pushes
python scripts/morning_push.py   # Pre-market recommendations
python scripts/evening_push.py   # End-of-day analysis
```

### Data Management
```bash
# Sync concept/theme data only
python scripts/_sync_concepts_only.py

# Train AI model only
python scripts/run_ai_model.py

# Enrich data (northbound flow, dragon-tiger list, sentiment)
python scripts/enhance_data.py

# Test factor effectiveness
python tools/factor_mining/factor_tester.py
```

### Testing
```bash
# Run unit tests (from project root)
python -m pytest tests/unit/

# Run a single test file
python -m pytest tests/unit/test_data_quality.py

# Integration test
python tests/integration/comprehensive_test.py
```

### Docker Deployment
```bash
# Build and start
docker-compose up -d

# Deploy to remote Linux server
python auto_deploy.py --host 192.168.3.22 --user li --port 22

# On the remote server
bash pull-and-deploy.sh
```

## Architecture

### Data Flow
```
External APIs (Tushare/eFinance/Baostock/AKShare)
    ↓
src/collector/data_loader.py  ← Multi-source with fallback
    ↓
Database (SQLite: data/quant.db  OR  MySQL: 192.168.3.41:3306)
    ↓
src/factors/alpha_engine.py   ← Computes 34 technical indicators
    ↓
scripts/run_ai_model.py       ← LightGBM training → ai_predictions table
    ↓
src/strategy/hybrid_strategy.py  ← Blends AI + Event + Fundamental scores
    ↓
output/hybrid_picks_YYYYMMDD.csv + DingTalk notification
```

### Configuration: `config/settings.yaml`
The single source of truth for all settings. Key sections:
- `tushare_token` — required for data collection
- `db_type` — `"sqlite"` or `"mysql"`; `mysql` section for connection details
- `strategy.topk` — number of stocks to select (default: 10 top, output 20)
- `event_driver.hot_topics` — list of Chinese themes to monitor (e.g. `"低空经济"`, `"人工智能"`)
- `notification.dingtalk.webhook` — DingTalk robot URL
- `industry_timing` — sector cycle configuration

All code accesses config via the `Config` singleton (`src/utils/config_loader.py`), which is a lazy-loaded `ConfigLoader` instance. Use `Config.get('section.key')` or attribute access `Config.tushare_token`.

### Database Abstraction: `src/utils/db_utils.py`
`DBUtils` transparently supports both SQLite and MySQL. Uses `Config.db_type` to decide. Key tables:
- `stock_daily` — OHLCV + PE/PB/ROE/market cap per stock per day
- `stock_info` — static stock metadata (ts_code stored WITH suffix, e.g. `000001.SZ`)
- `stock_concepts` — stock ↔ concept/theme mappings
- `stock_factors` — computed technical indicators
- `ai_predictions` — LightGBM output scores
- `stock_positions` — portfolio holdings

**MySQL placeholder handling**: `DBUtils.get_conn()` yields a `_MySQLConnWrapper` whose `cursor()` returns `_MySQLCursorWrapper`. This wrapper auto-converts SQLite `?` placeholders to MySQL `%s`, so all code using `with get_conn() as conn: cursor.execute("...?...", params)` works correctly without modification. Always use `DBUtils.query_df()` or `DBUtils.execute()` for new code — they also handle this conversion.

**Known schema note**: `stock_info.ts_code` uses the full Tushare format (`000001.SZ`), same as `stock_daily.ts_code`. JOINs should use `sd.ts_code = si.ts_code` directly, not strip-suffix logic.

### Key Source Modules

| Module | Purpose |
|--------|---------|
| `src/collector/data_loader.py` | Universal loader; tries primary API, falls back to secondary |
| `src/collector/data_quality.py` | Data completeness checks and anomaly detection |
| `src/factors/alpha_engine.py` | Calculates RSI, MACD, Bollinger Bands, and 31 other factors |
| `src/strategy/hybrid_strategy.py` | Combines AI + event + fundamental into final ranking |
| `src/strategy/topk_strategy.py` | TopK selection from scored stock pool |
| `src/analysis/event_driver.py` | Maps hot topics → concept stocks |
| `src/analysis/industry_timing.py` | Sector penetration-rate and economic-cycle signals |
| `src/portfolio/position_manager.py` | Position sizing, stop-loss (-8%), circuit breaker (>3% drop) |
| `src/utils/llm_client.py` | OpenAI/Anthropic integration for deep stock analysis |
| `src/utils/notifier.py` | DingTalk webhook push |
| `src/app/dashboard.py` | Streamlit UI — 交易驾驶舱 (trading cockpit) |
| `scripts/daily_alpha_run.py` | Orchestrates the complete daily pipeline |

### Scheduled Automation
On Windows: `setup_scheduled_tasks.bat` (run as Administrator)
On WSL/Linux (recommended): `bash scripts/wsl/install_cron.sh`

Default schedule:
- **8:00** — data sync + stock selection (`daily_alpha_run.py`)
- **8:30** — morning push (`morning_push.py`)
- **16:00** — evening push (`evening_push.py`)

### Deployment Topology
```
Windows (development) → Gitea (192.168.3.22:3000) → Linux server (192.168.3.22)
                                                            ↓ Docker/直连
                                                     MySQL (192.168.3.41:3306)
                                                            ↓
                                                     DingTalk notifications
```

### Server Info
- **Linux**: 192.168.3.22 / user: `li` / password: `bright7709`
- **SSH免密**: `ssh -i ~/.ssh/stock_deploy li@192.168.3.22`
- **项目目录**: `/home/li/robottrade`
- **部署命令**: `cd /home/li/robottrade && git pull gitea master`
- **重启服务**: `./venv/bin/python -m uvicorn src.web.main:app --host 0.0.0.0 --port 8080`

## Code Conventions

- **Chinese comments** are standard throughout this codebase — this is intentional and should be maintained.
- Scripts must be run from the **project root** so that relative imports (`from src.xxx import`) resolve correctly. `main.py` inserts the project root into `sys.path` at startup.
- The `Config` singleton reads `config/settings.yaml` relative to `src/utils/config_loader.py`. Do not change this path resolution.
- Proxy settings are **forcefully disabled** in `src/utils/network_utils.py` to prevent Tushare API failures through VPNs — do not re-enable them.
- To add a new data quality check, add it to `src/collector/data_quality.py` and optionally add a script under `tools/data_check/`.
