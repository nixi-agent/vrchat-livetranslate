@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   自检：不连 VRChat、不用麦克风，验证链路能不能通
echo ============================================
echo.

echo [1/4] 模块导入检查...
.venv\Scripts\python.exe -c "import vlt.app, vlt.session.qwen38, vlt.output.overlay, vlt.output.chatbox, vlt.output.merger; print('    OK 模块齐全')" || goto :err
echo.
echo [2/4] API key 检查...
.venv\Scripts\python.exe -c "from vlt.config import load_api_key; k=load_api_key(); print(f'    OK 读到 key: {k[:6]}***{k[-4:]} (长度 {len(k)})')" || goto :err
echo.
echo [3/4] 渲染检查（手腕屏贴图，不需要 VR）...
.venv\Scripts\python.exe -m vlt.output.overlay --out out\wrist_panel.png || goto :err
echo.
echo [4/4] 端到端检查（用自带测试音频跑一遍：中文 -> 英文，打进 chatbox）...
echo     如果你的 VRChat 正开着，气泡里会出现英文句子。
.venv\Scripts\python.exe -m vlt.app --direction mine --pcm testdata\zh_test_16k.pcm
echo.
echo ============================================
echo   自检结束
echo ============================================
pause
exit /b 0

:err
echo.
echo [X] 自检失败，请看上面的报错。
echo     常见原因：依赖没装全（重跑 setup.bat）／API key 没设／网络不通
pause
exit /b 1
