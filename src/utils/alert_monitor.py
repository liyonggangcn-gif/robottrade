"""
alert_monitor.py — 持仓估值异动 & profit_warnings 新增 推送监控

公开接口
--------
check_valuation_changes()   -> list[dict]   推送估值异动
check_profit_warning_changes() -> list[dict] 推送新增预警
run_all_alerts()            -> None         两项一起跑（供 daily_alpha_run 调用）
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from src.utils.db_utils import DBUtils
from src.utils.notifier import send_alert

# 升级方向：这些变化才值得推送（旧verdict → 新verdict 的集合）
ESCALATION_PAIRS: set[tuple[str, str]] = {
    # 变差（持仓需要关注）
    ("合理",   "高估"),
    ("合理",   "严重高估"),
    ("低估",   "高估"),
    ("低估",   "严重高估"),
    ("严重低估","高估"),
    ("严重低估","严重高估"),
    ("严重低估","合理"),
    # 变好（持仓利好，也推）
    ("高估",   "合理"),
    ("高估",   "低估"),
    ("高估",   "严重低估"),
    ("严重高估","合理"),
    ("严重高估","低估"),
    ("严重高估","严重低估"),
    ("数据不足","低估"),
    ("数据不足","严重低估"),
}


# ─── 快照存储 ─────────────────────────────────────────────────────────────────

def _ensure_snapshots_table():
    DBUtils.execute("""
        CREATE TABLE IF NOT EXISTS alert_snapshots (
            snap_key   VARCHAR(50) NOT NULL PRIMARY KEY,
            snap_json  MEDIUMTEXT  NOT NULL,
            updated_at TIMESTAMP   DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
        )
    """)


def _load_snapshot(key: str) -> dict | list | None:
    try:
        df = DBUtils.query_df(
            "SELECT snap_json FROM alert_snapshots WHERE snap_key = ?", [key]
        )
        if df.empty:
            return None
        return json.loads(df.iloc[0]["snap_json"])
    except Exception:
        return None


def _save_snapshot(key: str, data):
    _ensure_snapshots_table()
    DBUtils.execute(
        """INSERT INTO alert_snapshots (snap_key, snap_json)
           VALUES (?, ?)
           ON DUPLICATE KEY UPDATE snap_json = VALUES(snap_json),
                                   updated_at = CURRENT_TIMESTAMP""",
        [key, json.dumps(data, ensure_ascii=False)],
    )


# ─── 估值异动 ─────────────────────────────────────────────────────────────────

def check_valuation_changes() -> list[dict]:
    """
    对比持仓股票的估值 verdict 与上次快照，推送发生升/降级的股票。
    返回本次推送的异动列表。
    """
    _ensure_snapshots_table()

    # 当前持仓
    pos_df = DBUtils.query_df("SELECT ts_code, name FROM positions WHERE shares > 0")
    if pos_df.empty:
        return []
    codes = pos_df["ts_code"].tolist()
    name_map = {r["ts_code"]: r["name"] for _, r in pos_df.iterrows()}

    # 当前估值
    ph = ",".join(["?"] * len(codes))
    val_df = DBUtils.query_df(
        f"SELECT ts_code, verdict, upside_pct, val_method FROM valuation_cache WHERE ts_code IN ({ph})",
        codes,
    )
    if val_df.empty:
        return []

    current: dict[str, dict] = {
        r["ts_code"]: {
            "verdict":    str(r.get("verdict", "")),
            "upside_pct": r.get("upside_pct"),
            "val_method": str(r.get("val_method", "")),
        }
        for _, r in val_df.iterrows()
    }

    # 上次快照
    prev: dict = _load_snapshot("valuation_verdicts") or {}

    # 对比
    changes = []
    for code, cur in current.items():
        old_verdict = prev.get(code, {}).get("verdict", "")
        new_verdict = cur["verdict"]
        if not old_verdict or old_verdict == new_verdict:
            continue
        if (old_verdict, new_verdict) not in ESCALATION_PAIRS:
            continue
        changes.append({
            "ts_code":    code,
            "name":       name_map.get(code, code),
            "old_verdict": old_verdict,
            "new_verdict": new_verdict,
            "upside_pct":  cur.get("upside_pct"),
            "val_method":  cur.get("val_method"),
        })

    # 推送
    if changes:
        _send_valuation_alert(changes)

    # 更新快照（不管有无变化都更新，保持最新）
    _save_snapshot("valuation_verdicts", {
        code: {"verdict": v["verdict"]} for code, v in current.items()
    })

    return changes


def _send_valuation_alert(changes: list[dict]):
    VERDICT_EMOJI = {
        "严重高估": "🔴",
        "高估":     "🟠",
        "合理":     "🟡",
        "低估":     "🟢",
        "严重低估": "💎",
        "数据不足": "⚪",
    }
    lines = []
    for c in changes:
        old_e = VERDICT_EMOJI.get(c["old_verdict"], "")
        new_e = VERDICT_EMOJI.get(c["new_verdict"], "")
        upside = f"  上行空间 {c['upside_pct']:+.1f}%" if c.get("upside_pct") is not None else ""
        method = f"（{c['val_method']}）" if c.get("val_method") else ""
        direction = "⬆️ 改善" if c["new_verdict"] in ("低估", "严重低估", "合理") else "⬇️ 恶化"
        lines.append(
            f"- **{c['name']}**（{c['ts_code'][:6]}）{direction}\n"
            f"  {old_e}{c['old_verdict']} → {new_e}{c['new_verdict']}{method}{upside}"
        )
    content = (
        f"以下持仓股票估值发生变化（{datetime.now().strftime('%Y-%m-%d')}）：\n\n"
        + "\n".join(lines)
    )
    send_alert("📊 持仓估值异动", content, message_type="valuation_change")
    print(f"[估值异动] 已推送 {len(changes)} 只: "
          + ", ".join(f"{c['name']}({c['old_verdict']}→{c['new_verdict']})" for c in changes))


# ─── profit_warnings 新增 ─────────────────────────────────────────────────────

def check_profit_warning_changes() -> list[dict]:
    """
    对比 qmt.profit_warnings 与上次快照，推送新增预警。
    返回新增条目列表。
    """
    _ensure_snapshots_table()

    # 拉当前预警（未解除）
    current_warnings = _fetch_current_warnings()
    if current_warnings is None:
        return []

    # 上次快照（用 stock_name+level 作去重 key）
    prev_set: set[str] = set(_load_snapshot("profit_warnings") or [])

    # 对比：新增条目
    new_items = []
    current_keys = []
    for w in current_warnings:
        key = f"{w.get('stock_name','')}__{w.get('level','')}"
        current_keys.append(key)
        if key not in prev_set:
            new_items.append(w)

    # 推送新增
    if new_items:
        _send_warning_alert(new_items)

    # 更新快照
    _save_snapshot("profit_warnings", current_keys)

    return new_items


def _fetch_current_warnings() -> Optional[list[dict]]:
    try:
        import pymysql
        from src.utils.config_loader import Config
        mysql = Config.mysql if hasattr(Config, "mysql") else {}
        conn = pymysql.connect(
            host=mysql.get("host", "192.168.3.41"),
            port=int(mysql.get("port", 3306)),
            user=mysql.get("user", "root"),
            password=mysql.get("password", ""),
            database="qmt",
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT stock_code, stock_name, level, profit_change_pct, signals, warning_date "
            "FROM profit_warnings WHERE resolved_date IS NULL OR resolved_date = 'None'"
        )
        rows = cur.fetchall()
        conn.close()
        # 去重（stock_name+level 相同的只保留一条）
        seen: set = set()
        result = []
        for r in rows:
            key = f"{r.get('stock_name','')}__{r.get('level','')}"
            if key not in seen:
                seen.add(key)
                result.append(dict(r))
        return result
    except Exception as e:
        print(f"[profit_warnings] 读取失败: {e}")
        return None


def _send_warning_alert(new_items: list[dict]):
    LEVEL_EMOJI = {"红": "🔴", "黄": "🟡"}
    lines = []
    for w in new_items:
        level_str = str(w.get("level", ""))
        color = "红" if "红" in level_str else ("黄" if "黄" in level_str else "")
        emoji = LEVEL_EMOJI.get(color, "⚠️")
        pct = w.get("profit_change_pct")
        pct_str = f"  利润变动 {float(pct):+.1f}%" if pct is not None else ""
        sigs = str(w.get("signals", "")).strip("[]'\"")
        date_str = str(w.get("warning_date", ""))
        lines.append(
            f"- {emoji}**{w.get('stock_name', '')}** {level_str}{pct_str}\n"
            f"  触发信号：{sigs}  日期：{date_str}"
        )
    content = (
        f"QMT 新增 {len(new_items)} 条盈利预警：\n\n"
        + "\n".join(lines)
        + "\n\n> 建议关注相关持仓风险"
    )
    send_alert("⚠️ 盈利预警新增", content, message_type="profit_warning")
    print(f"[profit_warnings] 已推送 {len(new_items)} 条新增预警")


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def run_all_alerts() -> dict:
    """
    运行所有监控检查，返回结果摘要。
    供 daily_alpha_run.py 在估值刷新后调用。
    """
    results = {}
    try:
        val_changes = check_valuation_changes()
        results["valuation_changes"] = len(val_changes)
    except Exception as e:
        results["valuation_changes"] = f"ERROR: {e}"
        print(f"[AlertMonitor] 估值异动检查失败: {e}")

    try:
        warn_new = check_profit_warning_changes()
        results["new_warnings"] = len(warn_new)
    except Exception as e:
        results["new_warnings"] = f"ERROR: {e}"
        print(f"[AlertMonitor] profit_warnings 检查失败: {e}")

    return results
