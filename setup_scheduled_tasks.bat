@echo off
chcp 65001 >nul
echo ============================================================
echo   设置定时推送任务
echo ============================================================
echo.

set PROJECT_DIR=%~dp0
set PYTHON_PATH=python

echo [INFO] 项目目录: %PROJECT_DIR%
echo [INFO] Python路径: %PYTHON_PATH%
echo.

echo [步骤0] 创建数据同步+选股任务（03:00，凌晨运行确保开盘前数据同步完成）...
schtasks /create /tn "量化选股-数据同步" /tr "cmd /c \"cd /d %PROJECT_DIR% && %PYTHON_PATH% scripts\daily_alpha_run.py --skip-qlib\"" /sc daily /st 03:00 /f
if %errorlevel% equ 0 (echo [OK] 数据同步任务创建成功) else (echo [WARN] 数据同步任务创建失败或已存在)
echo.

echo [步骤1] 创建早盘推送任务（8:30）...
schtasks /create /tn "量化选股-早盘推送" /tr "cmd /c \"cd /d %PROJECT_DIR% && %PYTHON_PATH% scripts\morning_push.py\"" /sc daily /st 08:30 /f
if %errorlevel% equ 0 (
    echo [OK] 早盘推送任务创建成功
) else (
    echo [ERROR] 早盘推送任务创建失败
)
echo.

echo [步骤2] 创建收盘推送任务（16:00）...
schtasks /create /tn "量化选股-收盘推送" /tr "cmd /c \"cd /d %PROJECT_DIR% && %PYTHON_PATH% scripts\evening_push.py\"" /sc daily /st 16:00 /f
if %errorlevel% equ 0 (
    echo [OK] 收盘推送任务创建成功
) else (
    echo [ERROR] 收盘推送任务创建失败
)
echo.

echo ============================================================
echo   任务创建完成！
echo ============================================================
echo.
echo 查看任务列表:
schtasks /query /tn "量化选股-数据同步"
schtasks /query /tn "量化选股-早盘推送"
schtasks /query /tn "量化选股-收盘推送"
echo.
echo ============================================================
echo   管理命令:
echo   - 查看任务: schtasks /query /tn "量化选股-早盘推送"
echo   - 运行任务: schtasks /run /tn "量化选股-数据同步"
echo   - 运行任务: schtasks /run /tn "量化选股-早盘推送"
echo   - 删除任务: schtasks /delete /tn "量化选股-早盘推送" /f
echo ============================================================
echo.
pause
