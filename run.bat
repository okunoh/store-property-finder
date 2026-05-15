@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"C:\Users\okuno\AppData\Local\Programs\Python\Python312\python.exe" main.py >> logs\scheduler.log 2>&1
