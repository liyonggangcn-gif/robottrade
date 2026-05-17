@echo off
chcp 65001 >nul
title 管理量化选股服务

set SERVICE_NAME=QuantAgent选股服务

:MENU
cls
echo ============================================================
echo   量化选股服务管理
echo ============================================================
echo.
echo 请选择操作:
echo   1. 启动服务
echo   2. 停止服务
echo   3. 重启服务
echo   4. 查看服务状态
echo   5. 查看服务日志
echo   6. 卸载服务
echo   0. 退出
echo.
set /p choice=请输入选项 (0-6): 

if "%choice%"=="1" goto START
if "%choice%"=="2" goto STOP
if "%choice%"=="3" goto RESTART
if "%choice%"=="4" goto STATUS
if "%choice%"=="5" goto LOGS
if "%choice%"=="6" goto UNINSTALL
if "%choice%"=="0" goto EXIT

echo 无效选项，请重新选择
timeout /t 2 >nul
goto MENU

:START
echo.
echo [INFO] 正在启动服务...
net start "%SERVICE_NAME%"
if %errorlevel% equ 0 (
    echo [OK] 服务启动成功
) else (
    echo [ERROR] 服务启动失败
)
echo.
pause
goto MENU

:STOP
echo.
echo [INFO] 正在停止服务...
net stop "%SERVICE_NAME%"
if %errorlevel% equ 0 (
    echo [OK] 服务停止成功
) else (
    echo [ERROR] 服务停止失败
)
echo.
pause
goto MENU

:RESTART
echo.
echo [INFO] 正在重启服务...
net stop "%SERVICE_NAME%"
timeout /t 2 >nul
net start "%SERVICE_NAME%"
if %errorlevel% equ 0 (
    echo [OK] 服务重启成功
) else (
    echo [ERROR] 服务重启失败
)
echo.
pause
goto MENU

:STATUS
echo.
echo [INFO] 服务状态:
sc query "%SERVICE_NAME%"
echo.
pause
goto MENU

:LOGS
echo.
echo [INFO] 最近的日志文件:
dir /b /o-d logs\service_runner_*.log 2>nul | findstr /n "^" | more
echo.
set /p log_file=请输入要查看的日志文件名（直接回车查看最新的）: 
if "%log_file%"=="" (
    for /f "delims=" %%i in ('dir /b /o-d logs\service_runner_*.log 2^>nul') do (
        type "logs\%%i" | more
        goto MENU
    )
) else (
    if exist "logs\%log_file%" (
        type "logs\%log_file%" | more
    ) else (
        echo [ERROR] 文件不存在
    )
)
echo.
pause
goto MENU

:UNINSTALL
echo.
echo [WARN] 确定要卸载服务吗？这将删除Windows服务。
set /p confirm=输入 Y 确认卸载，其他键取消: 
if /i not "%confirm%"=="Y" goto MENU

echo.
echo [INFO] 正在卸载服务...

REM 尝试使用NSSM卸载
if exist "tools\nssm\win64\nssm.exe" (
    tools\nssm\win64\nssm.exe remove "%SERVICE_NAME%" confirm
    if %errorlevel% equ 0 (
        echo [OK] 使用NSSM卸载成功
        goto MENU
    )
)

REM 使用sc删除
sc stop "%SERVICE_NAME%" >nul 2>&1
timeout /t 2 >nul
sc delete "%SERVICE_NAME%"
if %errorlevel% equ 0 (
    echo [OK] 服务卸载成功
) else (
    echo [ERROR] 服务卸载失败，可能需要管理员权限
)
echo.
pause
goto MENU

:EXIT
exit
