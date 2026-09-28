@echo off
setlocal
cd /d "%~dp0"

py -3.12 -m venv .venv-pc 2>nul
if errorlevel 1 py -3 -m venv .venv-pc
if errorlevel 1 (
  echo Python 3.10+ not found.
  pause
  exit /b 1
)

call .venv-pc\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install "flet[desktop]==1.0.1" "requests>=2.32.0" "Pillow>=10.4.0" "beautifulsoup4>=4.12.0"
python desktop_main.py
