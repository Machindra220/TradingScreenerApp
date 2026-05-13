@echo off
:: 1. Force UTF-8 encoding to support emojis in logs/console
set PYTHONUTF8=1

:: 2. Navigate to the project root
cd /d "D:\Projects\portfolio-app-2-Dev-Env"

:: 3. Activate the virtual environment
:: Ensure the folder is named ".venv" and not "venv"
call "D:\Projects\portfolio-app-2-Dev-Env\.venv\Scripts\activate.bat"

:: 4. Run the WSGI script using the EXPLICIT path to the venv python
"D:\Projects\portfolio-app-2-Dev-Env\.venv\Scripts\python.exe" wsgi.py