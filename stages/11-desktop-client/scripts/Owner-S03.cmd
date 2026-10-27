@echo off
setlocal
title S03 Evidence
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Watch-S03Evidence.ps1" -RunDirectory "%~dp0evidence" -Machine A
echo.
echo Keep errors visible and report the message to Codex.
pause
endlocal
