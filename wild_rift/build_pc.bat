@echo off
setlocal
cd /d "%~dp0"

py -3.12 -m venv .venv-build-pc 2>nul
if errorlevel 1 py -3 -m venv .venv-build-pc
if errorlevel 1 (
  echo Python 3.10+ not found.
  pause
  exit /b 1
)

call .venv-build-pc\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install "flet[desktop,cli]==1.0.1" pyinstaller "requests>=2.32.0" "Pillow>=10.4.0" "beautifulsoup4>=4.12.0"

python -m py_compile db.py draft_matrix_engine.py engine.py sources.py updater.py package_updater.py item_text_utils.py main.py desktop_main.py adaptive_descriptions.py build_pc_release.py
if errorlevel 1 exit /b 1

python -m unittest -v test_regressions.py
if errorlevel 1 exit /b 1

python -c "from PIL import Image; im=Image.open('assets/icon.png').convert('RGBA'); im.save('assets/icon_windows.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 exit /b 1

if exist dist rmdir /s /q dist
if exist release rmdir /s /q release

for /f %%V in (VERSION.txt) do set APPVER=%%V
flet pack desktop_main.py --name WildRiftCounterAssistant --icon assets/icon_windows.ico --product-name "WR Counter Assistant" --file-description "Wild Rift Counter Assistant" --product-version "%APPVER%" --file-version "%APPVER%.0" --company-name "FARLINER" --yes
if errorlevel 1 exit /b 1

python build_pc_release.py
if errorlevel 1 exit /b 1

echo.
echo READY:
dir /b release\*.zip
pause
