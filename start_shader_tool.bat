@echo off
chcp 65001 >nul 2>&1
setlocal

cd /d "%~dp0"

set HOST=127.0.0.1
set PORT=8022

echo ============================================
echo   GLSL Shader 简化工具 - 一键启动
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请确认已安装并加入 PATH
    pause
    exit /b 1
)

echo [信息] 检查端口 %PORT% ...
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [警告] 端口 %PORT% 已被占用，尝试终止旧进程...
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
        taskkill /PID %%p /F >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
)

echo [信息] 启动服务: http://%HOST%:%PORT%/
echo [信息] 按 Ctrl+C 停止服务
echo.

start "" "http://%HOST%:%PORT%/"
python -m uvicorn app.main:app --host %HOST% --port %PORT%

echo.
echo [信息] 服务已停止
pause
