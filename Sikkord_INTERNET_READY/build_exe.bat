@echo off
setlocal
cd /d "%~dp0"
title SIKKORD PREMIUM V12 QT
python -m pip install --upgrade pip
python -m pip install -r requirements_client.txt
python -m PyInstaller --noconfirm --clean --onedir --windowed --name Sikkord ^
  --icon "assets\sikkord.ico" ^
  --add-data "assets;assets" ^
  --collect-all PySide6 ^
  client.py
echo.
echo ============================================================
echo HAZIR: dist\Sikkord\Sikkord.exe
echo NOT: Bu surum ONEDIR'dir. dist\Sikkord klasorunu komple gonder.
echo ============================================================
pause
