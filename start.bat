@echo off
rem Windows 一键启动
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv || goto :err
  .venv\Scripts\pip install -r requirements.txt || goto :err
)
.venv\Scripts\python -m app
goto :eof
:err
echo 启动失败:请确认已安装 Python 3.12+
pause
