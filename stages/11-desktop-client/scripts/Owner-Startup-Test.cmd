@echo off
setlocal
chcp 65001 >nul
set "S01_NODE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
if not exist "%S01_NODE%" set "S01_NODE=node.exe"
"%S01_NODE%" "%~dp0Owner-Startup-Test.cjs"
set "S01_RESULT=%ERRORLEVEL%"
echo.
echo Exit code: %S01_RESULT%
echo Keep this window open and report any error above.
pause
exit /b %S01_RESULT%
