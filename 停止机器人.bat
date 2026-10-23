@echo off
chcp 65001 >nul
setlocal

net session >nul 2>&1
if not errorlevel 1 goto :kill
echo Need administrator rights to kill leftover bot processes.
echo A UAC window will pop up, click YES.
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
exit /b

:kill
echo Killing all QQ bot processes ...
echo.
powershell -NoProfile -Command "$found = Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*run.py*' }; if ($found) { $found | ForEach-Object { Write-Host ('  kill PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } else { Write-Host '  (no visible bot process)' }"
taskkill /F /IM python3.13.exe /T >nul 2>nul
taskkill /F /IM python.exe /T >nul 2>nul
taskkill /F /IM pythonw.exe /T >nul 2>nul
echo.
echo Done. NapCat and QQ are NOT touched.
echo Now double-click the one-click launcher ONCE.
pause
