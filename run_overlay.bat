@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   别人说话 -> 中文显示在手腕屏（SteamVR overlay）
echo ============================================
echo   需要：VRChat 已运行 + SteamVR 已运行（否则 overlay 会自动禁用并提示）
echo   停止：按 Ctrl+C
echo.
echo 说明：采集的是 VRChat 的**播放输出**（WASAPI loopback）。
echo       必须在物理机当前会话里跑（远程桌面会话采不到声音）。
echo.
.venv\Scripts\python.exe -m vlt.app --direction theirs --loopback --sink overlay
pause
