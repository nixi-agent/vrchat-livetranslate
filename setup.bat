@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   VRChat 实时同传 - 环境安装
echo ============================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    echo [1/3] 用 py 启动器创建虚拟环境 ^(3.11^)...
    py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv
) else (
    echo [1/3] 用 python 创建虚拟环境...
    python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [X] 虚拟环境创建失败：请先安装 Python 3.11 并确保在 PATH 里
    echo     下载： https://www.python.org/downloads/release/python-3119/
    pause
    exit /b 1
)

echo [2/3] 升级 pip...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet

echo [3/3] 安装依赖 ^(约 1-2 分钟^)...
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo ============================================
echo   安装完成
echo ============================================
echo 下一步：
echo   1) 设置 API key（二选一）：
echo        setx DASHSCOPE_API_KEY "你的key"     然后重开一个命令行窗口
echo      或  bl auth login --api-key "你的key"
echo   2) 自检： run_selfcheck.bat
echo   3) 启动： run_chatbox.bat  或  run_overlay.bat
echo.
pause
