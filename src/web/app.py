#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QuantAgent-Alpha Web 管理界面
FastAPI + Jinja2，端口 8080
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
import uvicorn

app = FastAPI(title="QuantAgent-Alpha 管理后台", docs_url=None, redoc_url=None)
# 响应压缩：JSON/HTML > 500 字节时启用 gzip，可减少约 60-70% 传输量
app.add_middleware(GZipMiddleware, minimum_size=500)

# 挂载静态文件和模板
BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# 导入 API 路由
from src.web.api import router as api_router
app.include_router(api_router, prefix="/api")


# ── 页面路由 ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "page": "dashboard"})


@app.get("/picks")
async def picks_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/selection", status_code=302)


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request):
    return templates.TemplateResponse("positions.html", {"request": request, "page": "positions"})


@app.get("/sectors", response_class=HTMLResponse)
async def sectors(request: Request):
    return templates.TemplateResponse("sectors.html", {"request": request, "page": "sectors"})


@app.get("/news", response_class=HTMLResponse)
async def news(request: Request):
    return templates.TemplateResponse("news.html", {"request": request, "page": "news"})


@app.get("/sync", response_class=HTMLResponse)
async def sync(request: Request):
    return templates.TemplateResponse("sync.html", {"request": request, "page": "sync"})


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request, "page": "logs"})


@app.get("/advice", response_class=HTMLResponse)
async def advice(request: Request):
    return templates.TemplateResponse("advice.html", {"request": request, "page": "advice"})


@app.get("/config", response_class=HTMLResponse)
async def config(request: Request):
    return templates.TemplateResponse("config.html", {"request": request, "page": "config"})


@app.get("/pool", response_class=HTMLResponse)
async def pool(request: Request):
    return templates.TemplateResponse("selection.html", {"request": request, "page": "selection", "default_tab": "pool"})


@app.get("/analyze", response_class=HTMLResponse)
async def analyze(request: Request):
    return templates.TemplateResponse("analyze.html", {"request": request, "page": "analyze"})


@app.get("/etf", response_class=HTMLResponse)
async def etf(request: Request):
    return templates.TemplateResponse("selection.html", {"request": request, "page": "selection", "default_tab": "etf"})


@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request):
    return templates.TemplateResponse("selection.html", {"request": request, "page": "selection", "default_tab": "backtest"})


@app.get("/futures", response_class=HTMLResponse)
async def futures(request: Request):
    return templates.TemplateResponse("futures.html", {"request": request, "page": "futures"})


@app.get("/agent", response_class=HTMLResponse)
async def agent_dashboard(request: Request):
    return templates.TemplateResponse("agent.html", {"request": request, "page": "agent"})


@app.get("/agent/account", response_class=HTMLResponse)
async def agent_account_page(request: Request):
    return templates.TemplateResponse("agent_account.html", {"request": request, "page": "agent_account"})


@app.get("/agent/review", response_class=HTMLResponse)
async def agent_review_page(request: Request):
    return templates.TemplateResponse("agent_review.html", {"request": request, "page": "agent_review"})


@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page(request: Request):
    return templates.TemplateResponse("selection.html", {"request": request, "page": "selection"})


@app.get("/data_source", response_class=HTMLResponse)
async def data_source_page(request: Request):
    return templates.TemplateResponse("data_source.html", {"request": request, "page": "data_source"})


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    return templates.TemplateResponse("messages.html", {"request": request, "page": "messages"})


# ── 业务流核心页面 ─────────────────────────────────────────────────────────
@app.get("/selection", response_class=HTMLResponse)
async def selection_page(request: Request):
    from src.utils.db_utils import DBUtils
    from datetime import datetime
    
    latest_date = "2026-04-01"
    try:
        df = DBUtils.query_df("SELECT MAX(trade_date) as dt FROM stock_daily")
        if not df.empty:
            latest_date = str(df.iloc[0]['dt'])
    except:
        pass
    
    results = []
    history = []
    try:
        df = DBUtils.query_df(
            "SELECT * FROM daily_picks WHERE trade_date = ("
            "  SELECT MAX(trade_date) FROM daily_picks"
            ") ORDER BY final_score DESC LIMIT 50"
        )
        if not df.empty:
            df = df.fillna('')
            records = df.to_dict('records')
            for rec in records:
                for k, v in rec.items():
                    if hasattr(v, 'isoformat'):
                        rec[k] = v.isoformat()
                    elif hasattr(v, 'strftime'):
                        rec[k] = v.strftime('%Y-%m-%d %H:%M:%S')
                    elif isinstance(v, (float, int)):
                        rec[k] = v if v == v else None  # Handle NaN
                    elif v == '' or v is None:
                        rec[k] = None
            results = records
    except Exception as e:
        print(f"[selection_page] results error: {e}")
        pass
    try:
        from src.agent.multi_agent.memory_service import get_memory_service
        memory = get_memory_service()
        hist_df = memory.get_execution_history(days=30)
        if hist_df.shape[0] > 0:
            hist_records = hist_df.fillna('').to_dict('records')
            for rec in hist_records:
                for k, v in rec.items():
                    if hasattr(v, 'isoformat'):
                        rec[k] = v.isoformat()
                    elif hasattr(v, 'strftime'):
                        rec[k] = v.strftime('%Y-%m-%d %H:%M:%S')
                    elif isinstance(v, (float, int)):
                        rec[k] = v if v == v else None  # Handle NaN
                    elif v == '' or v is None:
                        rec[k] = None
            history = hist_records
        else:
            history = []
    except Exception as e:
        print(f"[selection_page] history error: {e}")
        pass
    
    return templates.TemplateResponse("selection.html", {
        "request": request, 
        "page": "selection",
        "latest_date": latest_date,
        "results": results,
        "history": history
    })


@app.get("/trading", response_class=HTMLResponse)
async def trading_page(request: Request):
    return templates.TemplateResponse("trading.html", {"request": request, "page": "trading"})


@app.get("/risk", response_class=HTMLResponse)
async def risk_page(request: Request):
    from src.agent.multi_agent.risk_agent import RiskAgent
    from datetime import datetime
    
    risk_level = "低"
    risk_summary = "无持仓或敞口为零"
    position_count = 0
    total_pnl = 0.0
    total_pnl_amount = 0
    win_rate = 0.0
    win_count = 0
    loss_count = 0
    stop_loss_signals = []
    take_profit_signals = []
    positions = []
    
    try:
        agent = RiskAgent()
        result = agent.run(trade_date=datetime.now().strftime("%Y-%m-%d"))
        
        assessment = result.get("risk_assessment", "")
        if "高风险" in assessment:
            risk_level = "高"
        elif "中等" in assessment or "中风险" in assessment:
            risk_level = "中"
        
        risk_summary = assessment
        sell_signals = result.get("sell_signals", [])
        stop_loss_signals = [s for s in sell_signals if s.get("signal") == "STOP_LOSS"]
        take_profit_signals = [s for s in sell_signals if s.get("signal") == "TAKE_PROFIT"]
        
        positions_df = agent.get_positions()
        if not positions_df.empty:
            position_count = len(positions_df)
            total_pnl = positions_df['profit_pct'].mean() * 100 if 'profit_pct' in positions_df.columns else 0
            total_pnl_amount = (positions_df['profit_pct'] * 100).sum() if 'profit_pct' in positions_df.columns else 0
            win_count = int((positions_df['profit_pct'] > 0).sum())
            loss_count = int((positions_df['profit_pct'] < 0).sum())
            win_rate = win_count / position_count * 100 if position_count > 0 else 0
            positions = positions_df.to_dict('records')
    except Exception as e:
        risk_summary = f"加载失败: {str(e)}"
    
    return templates.TemplateResponse("risk.html", {
        "request": request,
        "page": "risk",
        "risk_level": risk_level,
        "risk_summary": risk_summary,
        "position_count": position_count,
        "total_pnl": total_pnl,
        "total_pnl_amount": total_pnl_amount,
        "win_rate": win_rate,
        "win_count": win_count,
        "loss_count": loss_count,
        "stop_loss_signals": stop_loss_signals,
        "take_profit_signals": take_profit_signals,
        "positions": positions
    })


@app.get("/execution", response_class=HTMLResponse)
async def execution_page(request: Request):
    from src.agent.multi_agent.execution_agent import ExecutionAgent
    from src.utils.config_loader import Config
    
    total_assets = 1000000
    available_cash = 500000
    total_pnl = 0.0
    position_value = 0
    buy_orders = []
    sell_orders = []
    history = []
    
    try:
        cfg = Config.get('portfolio') or {}
        total_assets = cfg.get('initial_capital', 1000000)
        available_cash = cfg.get('stock_capital', 500000)
        
        agent = ExecutionAgent()
        buy_orders = []
        sell_orders = []
        available_cash = agent.get_available_cash()
        
        history = []
    except Exception as e:
        pass
    
    return templates.TemplateResponse("execution.html", {
        "request": request,
        "page": "execution",
        "total_assets": f"{total_assets:,.0f}",
        "available_cash": f"{available_cash:,.0f}",
        "total_pnl": total_pnl,
        "position_value": f"{position_value:,.0f}",
        "buy_orders": buy_orders,
        "sell_orders": sell_orders,
        "total_buy_amount": sum(o.get('amount', 0) for o in buy_orders),
        "history": history
    })


@app.get("/data_center", response_class=HTMLResponse)
async def data_center_page(request: Request):
    from src.utils.db_utils import DBUtils
    from datetime import datetime
    
    stats = {
        "stock_daily": 0,
        "stock_info": 0,
        "ai_predictions": 0,
        "stock_factors": 0,
        "push_messages": 0
    }
    latest_dates = {}
    
    try:
        for table in stats.keys():
            try:
                df = DBUtils.query_df(f"SELECT COUNT(*) as cnt, MAX(IF(trade_date IS NOT NULL, trade_date, updated_at)) as latest FROM {table}")
                if not df.empty:
                    stats[table] = int(df.iloc[0]['cnt'])
                    latest_dates[table] = str(df.iloc[0]['latest']) if df.iloc[0]['latest'] else "N/A"
            except:
                try:
                    df = DBUtils.query_df(f"SELECT COUNT(*) as cnt FROM {table}")
                    if not df.empty:
                        stats[table] = int(df.iloc[0]['cnt'])
                except:
                    pass
    except Exception as e:
        pass
    
    return templates.TemplateResponse("data_center.html", {
        "request": request,
        "page": "data_center",
        "stats": stats,
        "latest_dates": latest_dates
    })


@app.get("/cb", response_class=HTMLResponse)
async def cb_page(request: Request):
    return templates.TemplateResponse("selection.html", {"request": request, "page": "selection", "default_tab": "cb"})


# ── WebSocket 实时日志 ─────────────────────────────────────────────────────────
import asyncio
import glob
from datetime import datetime


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    log_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'logs')
    try:
        while True:
            # 找最新的日志文件
            today = datetime.now().strftime('%Y%m%d')
            patterns = [
                os.path.join(log_dir, f'*{today}*.log'),
                os.path.join(log_dir, '*.log'),
            ]
            files = []
            for p in patterns:
                files.extend(glob.glob(p))

            if files:
                latest = max(files, key=os.path.getmtime)
                try:
                    with open(latest, 'r', encoding='utf-8', errors='replace') as f:
                        f.seek(0, 2)  # 定位到文件末尾
                        while True:
                            line = f.readline()
                            if line:
                                await websocket.send_text(line.rstrip())
                            else:
                                await asyncio.sleep(0.5)
                except Exception:
                    await asyncio.sleep(2)
            else:
                await websocket.send_text("[等待日志文件...]")
                await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    uvicorn.run("src.web.app:app", host="0.0.0.0", port=8080, reload=False)
