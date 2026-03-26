@echo off
cd /d "%~dp0"
python run_mvp.py
if errorlevel 1 pause
