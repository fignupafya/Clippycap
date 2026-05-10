@echo off
title Clippycap
cd /d "%~dp0"
echo Starting Clippycap -- a browser tab should open in a few seconds.
echo (Keep this window open while you use the app; close it or press Ctrl+C to stop.)
echo.
".venv\Scripts\python.exe" -m clippycap %*
echo.
echo --- Clippycap has stopped. ---
pause
