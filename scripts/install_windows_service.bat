@echo off
chcp 65001 >nul
title 安装Windows服务

echo ============================================================
echo   安装量化选股系统为Windows服务
echo ============================================================
echo.

cd /d %~dp0\..

set PROJECT_DIR=%~dp0..
set PYTHON_PATH=python
set SERVICE_NAME=QuantAgent选股服务
set SERVICE_DISPLAY_NAME=量化选股系统服务
set SERVICE_DESCRIPTION=自动执行数据同步、选股和推送任务

echo [INFO] 项目目录: %PROJECT_DIR%
echo [INFO] Python路径: %PYTHON_PATH%
echo [INFO] 服务名称: %SERVICE_NAME%
echo.

echo ============================================================
echo   方法1: 使用NSSM安装（推荐，最简单）
echo ============================================================
echo.
echo 步骤1: 下载NSSM
echo   - 访问: https://nssm.cc/download
echo   - 下载最新版本（推荐64位）
echo   - 解压到项目目录下的 tools\nssm\ 文件夹
echo.
echo 步骤2: 安装服务
echo   运行: tools\nssm\win64\nssm.exe install "%SERVICE_NAME%" "%PYTHON_PATH%" "%PROJECT_DIR%\scripts\service_runner.py"
echo.
echo 步骤3: 配置服务
echo   运行: tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" AppDirectory "%PROJECT_DIR%"
echo   运行: tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" DisplayName "%SERVICE_DISPLAY_NAME%"
echo   运行: tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" Description "%SERVICE_DESCRIPTION%"
echo   运行: tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" Start SERVICE_AUTO_START
echo.
echo ============================================================
echo   方法2: 使用Python pywin32安装（需要管理员权限）
echo ============================================================
echo.
echo 步骤1: 安装依赖
echo   pip install pywin32
echo.
echo 步骤2: 运行安装脚本
echo   python scripts\install_service_pywin32.py
echo.
echo ============================================================
echo   管理命令
echo ============================================================
echo.
echo 启动服务:
echo   net start "%SERVICE_NAME%"
echo   或: sc start "%SERVICE_NAME%"
echo.
echo 停止服务:
echo   net stop "%SERVICE_NAME%"
echo   或: sc stop "%SERVICE_NAME%"
echo.
echo 查看服务状态:
echo   sc query "%SERVICE_NAME%"
echo.
echo 删除服务:
echo   sc delete "%SERVICE_NAME%"
echo   或使用NSSM: tools\nssm\win64\nssm.exe remove "%SERVICE_NAME%" confirm
echo.
echo ============================================================
echo   快速安装（如果已下载NSSM）
echo ============================================================
echo.

if exist "tools\nssm\win64\nssm.exe" (
    echo 检测到NSSM，开始安装服务...
    echo.
    
    echo [步骤1] 安装服务...
    tools\nssm\win64\nssm.exe install "%SERVICE_NAME%" "%PYTHON_PATH%" "%PROJECT_DIR%\scripts\service_runner.py"
    
    echo [步骤2] 配置服务...
    tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" AppDirectory "%PROJECT_DIR%"
    tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" DisplayName "%SERVICE_DISPLAY_NAME%"
    tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" Description "%SERVICE_DESCRIPTION%"
    tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" Start SERVICE_AUTO_START
    tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" AppStdout "%PROJECT_DIR%\logs\service_stdout.log"
    tools\nssm\win64\nssm.exe set "%SERVICE_NAME%" AppStderr "%PROJECT_DIR%\logs\service_stderr.log"
    
    echo.
    echo ============================================================
    echo   [OK] 服务安装完成！
    echo ============================================================
    echo.
    echo 下一步:
    echo   1. 启动服务: net start "%SERVICE_NAME%"
    echo   2. 查看日志: type logs\service_runner_*.log
    echo   3. 查看服务状态: sc query "%SERVICE_NAME%"
    echo.
) else (
    echo [WARN] 未找到NSSM，请先下载并解压到 tools\nssm\win64\ 目录
    echo.
    echo 下载地址: https://nssm.cc/download
    echo.
    echo 或者使用Python pywin32方式安装（需要先安装: pip install pywin32）
    echo   然后运行: python scripts\install_service_pywin32.py
)

pause
