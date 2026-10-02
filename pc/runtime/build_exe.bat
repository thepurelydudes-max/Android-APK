@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "LOG=build.log"
>"%LOG%" echo PC build started %date% %time%
where python >nul 2>nul || (echo Python 3.12 x64 is required.& exit /b 1)
if not exist .venv-build\Scripts\python.exe python -m venv .venv-build >>"%LOG%" 2>&1 || goto :err
set "BPY=.venv-build\Scripts\python.exe"
"%BPY%" -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')*8==64" >>"%LOG%" 2>&1 || goto :err
"%BPY%" -m pip install --upgrade pip >>"%LOG%" 2>&1 || goto :err
"%BPY%" -m pip install -r requirements-build.txt >>"%LOG%" 2>&1 || goto :err
"%BPY%" -m unittest discover -s tests -v >>"%LOG%" 2>&1 || goto :err
"%BPY%" build_release.py >>"%LOG%" 2>&1 || goto :err
type "%LOG%"
echo BUILD OK
exit /b 0
:err
echo BUILD FAILED.
type "%LOG%"
exit /b 1
