@echo off
powershell.exe -ExecutionPolicy Bypass -File "%~dp0ffmpeg.ps1"
echo.
echo The script has finished. Press any key to exit.
pause >nul
