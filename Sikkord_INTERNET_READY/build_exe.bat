@echo off
title SIKKORD FINAL EXE
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Sikkord client.py
echo.
echo HAZIR: dist\Sikkord.exe
pause
