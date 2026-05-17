# Scripts 目录说明

本目录包含所有可执行的脚本文件。

## 主要脚本

### 1. `start_all_services.py` - 一键启动所有服务
启动所有服务并运行今日选股，包括：
- 数据同步
- 因子更新
- TopK选股
- 小市值选股
- 交易信号生成
- Web界面启动

**使用方法：**
```bash
# 基本用法
python scripts\start_all_services.py

# 启动前检查并修复服务状态（推荐）
python scripts\start_all_services.py --check-services

# 跳过服务检查
python scripts\start_all_services.py --skip-check
```

**参数说明：**
- `--check-services`: 启动前检查并修复服务状态（定时任务等）
- `--skip-check`: 跳过服务检查

### 2. `check_and_fix_services.py` - 服务检查和修复
检查所有服务的运行状态，包括：
- Windows计划任务状态
- 推送脚本存在性
- 通知配置
- 推送功能测试
- 日志文件检查

**使用方法：**
```bash
python scripts\check_and_fix_services.py
```

### 3. `create_scheduled_tasks.py` - 创建定时任务
创建Windows计划任务：
- 量化选股-数据同步（每天8:00）
- 量化选股-早盘推送（每天8:30）
- 量化选股-收盘推送（每天16:00）

**使用方法：**
```bash
python scripts\create_scheduled_tasks.py
```

### 4. `service_monitor.py` - 服务监控脚本
可作为Windows服务运行，自动执行定时任务。

**使用方法：**
```bash
# 直接运行
python scripts\service_monitor.py

# 或作为Windows服务运行（使用NSSM）
# 参考 docs/WINDOWS_SERVICE_SETUP.md
```

### 5. `morning_push.py` - 早盘推送
发送早盘推送通知（包含选股结果和仓位分配）。

**使用方法：**
```bash
python scripts\morning_push.py
```

### 6. `evening_push.py` - 收盘推送
发送收盘推送通知（包含持仓分析和市场概况）。

**使用方法：**
```bash
python scripts\evening_push.py
```

### 7. `daily_alpha_run.py` - 每日数据同步和选股
执行每日数据同步、AI评分、选股和推送。

**使用方法：**
```bash
python scripts\daily_alpha_run.py
```

## 集成使用

### 推荐流程

1. **首次启动或定期检查**：
   ```bash
   python scripts\start_all_services.py --check-services
   ```
   这会先检查服务状态，自动修复缺失的定时任务，然后启动所有服务。

2. **日常启动**：
   ```bash
   python scripts\start_all_services.py
   ```
   直接启动所有服务，不进行服务检查（更快）。

3. **单独检查服务**：
   ```bash
   python scripts\check_and_fix_services.py
   ```

4. **手动创建定时任务**：
   ```bash
   python scripts\create_scheduled_tasks.py
   ```

## 注意事项

1. **管理员权限**：创建定时任务需要管理员权限
2. **Python路径**：确保Python在系统PATH中
3. **工作目录**：脚本会自动设置正确的工作目录
4. **日志文件**：所有脚本的日志保存在 `logs/` 目录下

## 故障排除

如果遇到问题：

1. **运行服务检查**：
   ```bash
   python scripts\check_and_fix_services.py
   ```

2. **查看日志**：
   ```bash
   # 查看今天的日志
   Get-Content logs\start_all_services_$(Get-Date -Format 'yyyyMMdd').log -Tail 50
   ```

3. **手动测试推送**：
   ```bash
   python scripts\evening_push.py
   ```

4. **重新创建定时任务**：
   ```bash
   python scripts\create_scheduled_tasks.py
   ```
