@echo off
chcp 65001 >nul
cd /d %~dp0
echo 安装提交前凭据扫描钩子...
if not exist ".git\hooks" mkdir ".git\hooks"
(
echo #!/bin/sh
echo # 提交前拦下含凭据的提交（由 install_secret_guard.bat 生成）
echo python scripts/check_no_secrets.py
) > ".git\hooks\pre-commit"
echo.
echo 已安装 .git\hooks\pre-commit
echo 测试：随便往一个文件里写个假 key 再 git commit，应该会被拦下。
echo.
pause
