@echo off
REM Builds ADBFileTransfer.exe from adb_transfer.py
REM Put this file in the same folder as adb_transfer.py, then double-click it.
REM Requires Python 3.8+ installed (with "Add Python to PATH" ticked).

python -m pip install --upgrade pyinstaller qrcode windnd
if errorlevel 1 goto :fail

python -m PyInstaller --onefile --noconsole --name ADBFileTransfer adb_transfer.py
if errorlevel 1 goto :fail

echo.
echo Done. Your app is at: dist\ADBFileTransfer.exe
pause
exit /b 0

:fail
echo.
echo Build failed. Check the messages above.
pause
exit /b 1
