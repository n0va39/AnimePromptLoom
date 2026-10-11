@echo off
cd /d "%~dp0"
python app.py --open --start-ollama
if errorlevel 1 pause
