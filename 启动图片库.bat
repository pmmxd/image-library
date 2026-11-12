@echo off
if not defined MEDIA_LIBRARY_ROOT (
  echo Please set MEDIA_LIBRARY_ROOT to your image library root.
  pause
  exit /b 1
)
if not defined LOCALDOCK_EXE (
  echo Please set LOCALDOCK_EXE to the full path of LocalDock.exe.
  pause
  exit /b 1
)
cd /d "%~dp0"
python start_library.py --root "%MEDIA_LIBRARY_ROOT%" --localdock "%LOCALDOCK_EXE%"
if errorlevel 1 pause
