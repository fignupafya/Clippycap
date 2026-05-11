@echo off
title Clippycap
cd /d "%~dp0"
echo Starting Clippycap...  (close the app window to stop)
".venv\Scripts\python.exe" -m clippycap %*
if errorlevel 1 pause
