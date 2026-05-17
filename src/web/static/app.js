/**
 * QuantAgent-Alpha Web 管理界面 - 公共 JS 工具库
 */

// ─── API 工具 ───────────────────────────────────────────────────────────────
const API = {
    get: (url) => fetch(url).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }),
    post: (url, data = {}) => fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }),
    postQuery: (url, params = {}) => {
        // POST with query string parameters
        const qs = new URLSearchParams(params).toString();
        return fetch(qs ? `${url}?${qs}` : url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        }).then(r => r.json());
    },
};

// ─── 操作触发（带 loading 状态）────────────────────────────────────────────
async function runAction(url, btnEl, btnLabel, isPostQuery = false, queryParams = {}) {
    const origHTML = btnEl ? btnEl.innerHTML : '';
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>执行中...';
    }
    try {
        let r;
        if (isPostQuery) {
            r = await API.postQuery(url, queryParams);
        } else {
            r = await API.post(url);
        }
        showToast(r.success !== false ? 'success' : 'danger', r.message || r.error || '操作完成');
    } catch (e) {
        showToast('danger', '请求失败: ' + e.message);
    } finally {
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.innerHTML = btnLabel || origHTML;
        }
    }
}

// ─── Bootstrap Toast 通知 ────────────────────────────────────────────────────
function showToast(type, msg) {
    const id = 'toast-' + Date.now();
    const iconMap = {
        success: 'bi-check-circle-fill',
        danger: 'bi-x-circle-fill',
        warning: 'bi-exclamation-triangle-fill',
        info: 'bi-info-circle-fill',
    };
    const icon = iconMap[type] || 'bi-info-circle-fill';
    const html = `
        <div id="${id}" class="toast align-items-center border-0 text-bg-${type}" role="alert" aria-live="assertive">
            <div class="d-flex">
                <div class="toast-body d-flex align-items-center gap-2">
                    <i class="bi ${icon}"></i>${msg}
                </div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
            </div>
        </div>`;
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
        container.style.zIndex = '9999';
        document.body.appendChild(container);
    }
    container.insertAdjacentHTML('beforeend', html);
    const el = document.getElementById(id);
    const toast = new bootstrap.Toast(el, { delay: 4000 });
    toast.show();
    el.addEventListener('hidden.bs.toast', () => el.remove());
}

// ─── 盈亏格式化（A股：涨红跌绿）────────────────────────────────────────────
function formatPnl(val, suffix = '%') {
    const n = parseFloat(val);
    if (isNaN(n)) return '<span class="neutral">-</span>';
    const cls = n > 0 ? 'profit' : n < 0 ? 'loss' : 'neutral';
    const sign = n > 0 ? '+' : '';
    return `<span class="${cls}">${sign}${n.toFixed(2)}${suffix}</span>`;
}

// ─── 数字格式化 ──────────────────────────────────────────────────────────────
function formatWan(val) {
    const n = parseFloat(val);
    if (isNaN(n) || n === 0) return '-';
    if (Math.abs(n) >= 100000000) return (n / 100000000).toFixed(2) + '亿';
    return (n / 10000).toFixed(1) + '万';
}

function formatNumber(val, decimals = 2) {
    const n = parseFloat(val);
    if (isNaN(n)) return '-';
    return n.toFixed(decimals);
}

// ─── 评分进度条 ──────────────────────────────────────────────────────────────
function scoreBar(val, max = 100) {
    const pct = Math.min(100, Math.max(0, (parseFloat(val) / max) * 100)) || 0;
    return `
        <div class="d-flex align-items-center gap-2">
            <div class="score-bar flex-grow-1">
                <div class="score-bar-inner" style="width:${pct.toFixed(1)}%"></div>
            </div>
            <span style="font-size:12px;min-width:32px">${parseFloat(val).toFixed(1)}</span>
        </div>`;
}

// ─── 新闻来源徽章 ────────────────────────────────────────────────────────────
function sourceBadge(source) {
    const s = (source || '').toLowerCase();
    let cls = 'source-other';
    let label = source || '未知';
    if (s.includes('cls') || s.includes('财联社')) { cls = 'source-cls'; label = '财联社'; }
    else if (s.includes('eastmoney') || s.includes('东方财富') || s.includes('em')) { cls = 'source-em'; label = '东方财富'; }
    else if (s.includes('sina') || s.includes('新浪')) { cls = 'source-sina'; label = '新浪财经'; }
    else if (s.includes('ths') || s.includes('同花顺')) { cls = 'source-ths'; label = '同花顺'; }
    else if (s.includes('wsj') || s.includes('wallstreet') || s.includes('华尔街')) { cls = 'source-wsj'; label = '华尔街见闻'; }
    return `<span class="source-badge ${cls}">${label}</span>`;
}

// ─── 状态徽章 ────────────────────────────────────────────────────────────────
function statusBadge(ok, trueLabel = '正常', falseLabel = '异常') {
    return ok
        ? `<span class="badge bg-success">${trueLabel}</span>`
        : `<span class="badge bg-danger">${falseLabel}</span>`;
}

// ─── 日志行着色 ──────────────────────────────────────────────────────────────
function colorLogLine(line) {
    const escaped = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const l = line.toLowerCase();
    if (l.includes('error') || l.includes('错误') || l.includes('失败') || l.includes('exception')) {
        return `<span class="log-line log-error">${escaped}</span>`;
    }
    if (l.includes('warn') || l.includes('warning') || l.includes('警告')) {
        return `<span class="log-line log-warn">${escaped}</span>`;
    }
    if (l.includes('info') || l.includes('成功') || l.includes('完成')) {
        return `<span class="log-line log-info">${escaped}</span>`;
    }
    return `<span class="log-line log-debug">${escaped}</span>`;
}

// ─── 加载占位符 ──────────────────────────────────────────────────────────────
function loadingHTML(msg = '加载中...') {
    return `<div class="text-center py-4 text-muted"><span class="spinner-border spinner-border-sm me-2"></span>${msg}</div>`;
}

function errorHTML(msg) {
    return `<div class="alert alert-danger alert-sm py-2"><i class="bi bi-exclamation-triangle me-1"></i>${msg}</div>`;
}

function emptyHTML(msg = '暂无数据') {
    return `<div class="text-center py-4 text-muted"><i class="bi bi-inbox fs-3 d-block mb-2"></i>${msg}</div>`;
}
