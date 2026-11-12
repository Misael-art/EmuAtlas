@echo off
setlocal
"%~dp0windows\python\python.exe" -X utf8 "%~dp0..\tools\roms\portable_emulator.py" %*
exit /b %errorlevel%
