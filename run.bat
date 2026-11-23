@echo off
rem ===========================================================================
rem  run.bat - Windows launcher for bmeoj_archive.py
rem
rem  Double-click: interactive prompts for OJ user + target folder, then pause.
rem  Command line:  run.bat --user 11226128 --dir output --init-git
rem
rem  This launcher NEVER runs git push and NEVER creates a remote repository.
rem ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

set "PY="
if defined BMEOJ_PYTHON if exist "%BMEOJ_PYTHON%" set PY="%BMEOJ_PYTHON%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo [错误] 没有找到 Python 3。
    echo 请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/
    echo 安装时勾选 "Add python.exe to PATH"，或设置环境变量 BMEOJ_PYTHON 指向 python.exe。
    echo.
    exit /b 2
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] Python 版本过低，需要 3.8 或更高版本。
    %PY% --version
    echo.
    exit /b 2
)

rem 源码布局：脚本在根目录；发行包布局：脚本在 files\ 下。
set "SCRIPT=%~dp0bmeoj_archive.py"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0files\bmeoj_archive.py"
if not exist "%SCRIPT%" (
    echo.
    echo [错误] 找不到 bmeoj_archive.py。
    echo 请确认 run.bat 与 bmeoj_archive.py（或 files\bmeoj_archive.py）在同一个目录结构中。
    echo.
    exit /b 2
)

%PY% "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"

if "%~1"=="" (
    echo.
    echo 按任意键关闭窗口...
    pause >nul
)
exit /b %RC%
