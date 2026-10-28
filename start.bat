@echo off
rem Windows 一键启动:自动找/装 Python、建虚拟环境、装依赖、启动并打开浏览器
chcp 65001 >nul
setlocal EnableDelayedExpansion
title PDF 翻译工具
cd /d "%~dp0"

echo ==========================================
echo    PDF 翻译工具 - 一键启动
echo ==========================================
echo.

rem ---- 1. 查找 Python 3.11+ ----
set "PY="
set "PYA="
set "PYOLD="
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import tomllib" >nul 2>&1
    if not errorlevel 1 (set "PY=python") else (set "PYOLD=1")
)
if not defined PY (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -c "import tomllib" >nul 2>&1
        if not errorlevel 1 (set "PY=py" & set "PYA=-3")
    )
)

rem ---- 2. 没有可用 Python:询问是否自动下载安装 ----
if not defined PY (
    if defined PYOLD (echo [!] 检测到的 Python 版本过低,需要 3.11 以上。) else (echo [!] 未检测到 Python。)
    echo.
    set "ANS="
    set /p ANS=是否自动下载并安装 Python 3.12?仅安装到当前用户,无需管理员权限 [Y/N]:
    if /i not "!ANS!"=="Y" goto :nopython
    call :install_python
    if errorlevel 1 goto :nopython
)

echo [OK] Python: "!PY!"
echo.

rem ---- 3. 虚拟环境与依赖(依赖清单变化时自动补装) ----
if not exist ".venv\Scripts\python.exe" (
    echo 首次运行:创建虚拟环境并安装依赖,需要几分钟...
    "!PY!" !PYA! -m venv .venv || goto :err
    set "NEED_INSTALL=1"
) else (
    set "NEED_INSTALL=0"
    if not exist ".venv\.reqs.txt" (set "NEED_INSTALL=1")
)
if "!NEED_INSTALL!"=="0" (
    fc /b ".venv\.reqs.txt" "requirements.txt" >nul 2>&1
    if errorlevel 1 (set "NEED_INSTALL=1")
)
if "!NEED_INSTALL!"=="1" (
    .venv\Scripts\python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 官方源较慢或失败,改用清华镜像重试...
        .venv\Scripts\python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || goto :err
    )
    copy /y "requirements.txt" ".venv\.reqs.txt" >nul
)

rem ---- 4. 启动(浏览器会自动打开) ----
echo.
echo 启动中...浏览器将自动打开 http://127.0.0.1:8765
echo 关闭本窗口或按 Ctrl+C 即停止服务。
echo.
.venv\Scripts\python -m app
echo.
echo 服务已停止。
pause
exit /b 0

:install_python
set "PYEXE=%TEMP%\python-3.12.10-amd64.exe"
echo 正在下载 Python 3.12 安装包,约 26MB,请稍候...
curl -fL --connect-timeout 15 -o "%PYEXE%" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
if errorlevel 1 (
    echo 官方源连接失败,尝试华为云镜像...
    curl -fL --connect-timeout 15 -o "%PYEXE%" "https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe"
)
if not exist "%PYEXE%" (
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%PYEXE%' -UseBasicParsing" >nul 2>&1
)
set "SZ=0"
if exist "%PYEXE%" (for %%F in ("%PYEXE%") do set "SZ=%%~zF")
if !SZ! LSS 1000000 (
    echo [X] Python 下载失败:请检查网络,或手动安装 https://www.python.org/downloads/
    exit /b 1
)
echo 下载完成,正在静默安装(约 1 分钟)...
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
del "%PYEXE%" >nul 2>&1
set "CAND=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "!CAND!" (
    set "PY=!CAND!"
    set "PYA="
    exit /b 0
)
echo [X] 安装完成但未找到 python.exe,请关闭窗口后重新双击本文件再试。
exit /b 1

:nopython
echo.
echo 已取消自动安装。可手动安装 Python 3.12+ 后再次双击本文件:
echo     https://www.python.org/downloads/
pause
exit /b 1

:err
echo.
echo [X] 依赖安装失败:请检查网络后重试;国内网络会自动改用清华镜像。
pause
exit /b 1
