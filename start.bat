@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo [INFO] .env created. Fill in DEEPSEEK_API_KEY, then run this again.
    notepad ".env"
    exit /b 0
)

set "PY=python"
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist ".deps" (
    set "PYTHONPATH=%~dp0.deps"
)

echo Using: %PY%
"%PY%" run.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo Exit code %CODE%. If it says ModuleNotFoundError, run install.bat first.
    pause
)
endlocal & exit /b %CODE%
