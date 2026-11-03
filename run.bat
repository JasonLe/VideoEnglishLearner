@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [x] 没找到 .venv
    echo     先按 README 的「快速开始」建虚拟环境并装依赖。
    pause
    exit /b 1
)

echo 正在启动 vel（VideoEnglishLearner）...
echo 用 Ctrl+C 停止
echo.
".venv\Scripts\python.exe" -m app.main
echo.
echo 服务已退出。
pause
