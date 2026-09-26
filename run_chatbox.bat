@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   我说中文 -> 译文进 VRChat chatbox 气泡
echo ============================================
echo   需要：VRChat 已运行 + OSC 已开
echo   停止：按 Ctrl+C
echo.

if "%DASHSCOPE_API_KEY%"=="" (
    if not exist "%USERPROFILE%\.bailian\config.json" (
        echo [X] 没找到 API key。二选一：
        echo       setx DASHSCOPE_API_KEY "你的key"   ^(然后重开窗口^)
        echo       bl auth login --api-key "你的key"
        pause
        exit /b 1
    )
)

echo 目标语言在 config.yaml 的 directions.mine.target_lang 里改（默认 en）
echo Mic device: set capture.mic_device in config.yaml (or use the GUI settings dialog). Use --list-devices to see available devices
echo.
.venv\Scripts\python.exe -m vlt.app --direction mine --mic --sink chatbox --list-devices
echo.
echo 上面是设备列表。按任意键开始采集（默认设备）...
pause >nul
.venv\Scripts\python.exe -m vlt.app --direction mine --mic --sink chatbox
pause
