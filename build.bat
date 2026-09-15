@echo off
setlocal
cd /d "%~dp0"

set "PY=py -3"
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 set "PY=python"

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if errorlevel 1 (
  echo Fun Tab needs Python 3.10 or newer on PATH to build.
  pause
  exit /b 1
)

echo Installing build tools...
%PY% -m pip install -e ".[build]"
if errorlevel 1 (
  echo Could not install PyInstaller.
  pause
  exit /b 1
)

echo.
echo Packing FunTab.exe ...
%PY% -m PyInstaller --noconfirm --clean fun_tab.spec
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

if exist "dist\FunTab.zip" del "dist\FunTab.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\FunTab\*' -DestinationPath 'dist\FunTab.zip'"
if errorlevel 1 (
  echo Packed dist\FunTab\FunTab.exe but zipping failed.
  pause
  exit /b 1
)

echo.
echo Give people dist\FunTab.zip
echo They unzip it and double-click FunTab.exe - no Python, no pip.
echo Keep FunTab.exe next to the _internal folder.
exit /b 0
