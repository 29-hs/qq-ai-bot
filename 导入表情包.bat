@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "DEST=%~dp0stickers"
if not exist "%DEST%" mkdir "%DEST%"

if "%~1"=="" (
    echo [INFO] No files given. Opening the stickers folder ...
    start "" "%DEST%"
    echo.
    echo   How to use:
    echo     1. Drag image files ^(png / jpg / gif / webp^) onto this .bat
    echo     2. Or copy them into the folder that just opened
    echo     3. Restart the bot afterwards
    pause
    exit /b 0
)

set /a count=0
set /a failed=0
:loop
if "%~1"=="" goto done
echo Copying "%~nx1" ...
copy /y "%~1" "%DEST%\" >nul 2>nul
if errorlevel 1 (
    set /a failed+=1
    echo    [FAILED]
) else (
    set /a count+=1
    echo    [OK]
)
shift
goto loop

:done
echo.
echo Imported !count! file^(s^), failed !failed!.
echo.
echo   The file name ^(without extension^) is the trigger word.
echo   Example: "happy.png" -^> the bot can send [tu:happy]
echo   Tip: rename to short Chinese words like kaixin / wuyu before importing.
echo   Restart the bot to load new stickers.
pause
