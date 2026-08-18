@echo off
setlocal
cd /d "%~dp0"
title SIKKORD STABLE V8 EXE
python -m pip install -r requirements_client.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Sikkord --collect-all pystray client.py
echo.
echo ================================================
echo HAZIR: dist\Sikkord.exe
echo ================================================
pause
