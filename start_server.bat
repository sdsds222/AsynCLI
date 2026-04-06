@echo off
chcp 65001 >nul
title AsynCLI Server (Agent-CLI-Broker)
echo =========================================
echo 正在启动 AsynCLI 服务端...
echo =========================================
python server.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo 服务端异常退出，请检查是否已安装所需依赖 (如 pexpect 等)
    pause
)
