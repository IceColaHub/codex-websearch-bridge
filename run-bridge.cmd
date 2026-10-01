@echo off
setlocal
set "DIR=%~dp0"

rem 优先用 pythonw（不弹黑窗）；没装再退回 python
set "PY=pythonw"
where pythonw >nul 2>nul || set "PY=python"
where %PY% >nul 2>nul || (
  echo [bridge] 没找到 Python，请先安装 Python 并把 pythonw / python 加进 PATH
  exit /b 1
)

:loop
"%PY%" "%DIR%web_search_bridge.py"

rem 退出码 1 = 配置或安全检查没过，别无限重启，停在这里等人工处理
if "%errorlevel%"=="1" (
  echo [bridge] 启动失败，详情见 %DIR%bridge.log
  exit /b 1
)

rem 其它退出（比如端口被占、意外崩）等 5 秒重来
timeout /t 5 /nobreak >nul
goto loop
