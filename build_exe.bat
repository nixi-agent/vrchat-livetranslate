@echo off
REM ============================================================
REM  双击本文件即可把界面打包成单文件 exe（无控制台窗口）
REM  产物：dist\VRChatLiveTranslate.exe
REM  加 --no-verify 可跳过打完之后的自动自检
REM ============================================================
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [X] 没找到 .venv\Scripts\python.exe
    echo     请先按 README 建好虚拟环境并安装依赖，再跑本脚本。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "scripts\build_exe.py" %*
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
    echo [OK] 打包完成 —— 产物在 dist\ 目录，整个文件夹一起发给别人即可。
) else (
    echo [X] 打包或自检失败，退出码 %RC%
)
pause
exit /b %RC%
