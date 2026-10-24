@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if exist ".venv\Scripts\python.exe" goto run

echo 首次启动：正在创建运行环境，请稍等…
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo 没有找到 Python。请先安装 Python 3.10 或更高版本，再双击本文件。
  pause
  exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo.
  echo Python 版本太低，需要 3.10 或更高版本。
  pause
  exit /b 1
)

python -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto fail

:run
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, PIL, multipart" >nul 2>nul
if errorlevel 1 (
  echo 正在安装运行依赖，请稍等…
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" app.py
pause
exit /b 0

:fail
echo.
echo 依赖安装失败，请检查网络后重试。
pause
exit /b 1
