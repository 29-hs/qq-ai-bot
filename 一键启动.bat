@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title QQ AI Bot - One Click Start

echo ============================================================
echo   QQ AI Bot - one click start
echo ============================================================
echo.

netstat -ano | findstr /C:":3001 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    echo [1/3] NapCat is already running on port 3001, skip launching it.
    goto :bot
)

echo [1/3] Starting NapCat ...
echo       A UAC window will pop up, click YES.
if not exist "D:\napcat\launcher.bat" (
    echo [ERROR] D:\napcat\launcher.bat not found.
    pause
    exit /b 1
)
if exist "D:\napcat\cache\qrcode.png" del /q "D:\napcat\cache\qrcode.png" >nul 2>nul
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath 'D:\napcat\launcher.bat' -WorkingDirectory 'D:\napcat'"
if errorlevel 1 (
    echo [ERROR] Failed to start NapCat. Did you cancel the UAC prompt?
    pause
    exit /b 1
)

echo [2/3] Waiting for NapCat WebSocket on port 3001 ...
echo       If a QR code is needed, it will pop up as an image automatically.
set /a tries=0
:wait
timeout /t 2 /nobreak >nul
netstat -ano | findstr /C:":3001 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 goto :ready
if not defined QROPEN (
    if exist "D:\napcat\cache\qrcode.png" (
        echo       QR code found, opening the image - scan it from the screen.
        start "" "D:\napcat\cache\qrcode.png"
        set "QROPEN=1"
    )
)
set /a tries+=1
if !tries! GEQ 150 (
    echo [WARN] Port 3001 still closed after 5 minutes.
    echo        Check the NapCat window: logged in? WebSocket server enabled?
    echo        The bot will start anyway and keep retrying.
    goto :bot
)
set /a rest=!tries! %% 15
if !rest! EQU 0 echo       ... still waiting ^(!tries! x 2s^)
goto :wait

:ready
echo [2/3] Port 3001 is up.
echo.

:bot
echo [3/3] Starting bot ...
echo.
call "%~dp0start.bat" %*
endlocal
