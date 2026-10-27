@echo off
setlocal
title S02 Isolated Test
cd /d "%~dp0"
set "S02_NODE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
if not exist "%S02_NODE%" set "S02_NODE=node"
"%S02_NODE%" "..\..\..\stages\11-desktop-client\scripts\Owner-S02-Test.cjs"
if errorlevel 1 goto done
start "" "http://127.0.0.1:4175/__control"
:done
echo.
echo Keep error text visible and report it to Codex.
pause
endlocal
