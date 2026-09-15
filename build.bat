@echo off
setlocal
cd /d "%~dp0"

set "PY=py -3"
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 set "PY=python"

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if errorlevel 1 (
  echo Fun Tab needs Python 3.10 or newer on PATH to build.
  if not defined CI pause
  exit /b 1
)

echo Installing build tools...
%PY% -m pip install -e ".[build]"
if errorlevel 1 (
  echo Could not install PyInstaller.
  if not defined CI pause
  exit /b 1
)

echo.
echo Packing FunTab.exe ...
%PY% -m PyInstaller --noconfirm --clean fun_tab.spec
if errorlevel 1 (
  echo Build failed.
  if not defined CI pause
  exit /b 1
)

if exist "dist\FunTab.zip" del "dist\FunTab.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\FunTab\*' -DestinationPath 'dist\FunTab.zip'"
if errorlevel 1 (
  echo Packed dist\FunTab\FunTab.exe but zipping failed.
  if not defined CI pause
  exit /b 1
)

set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if defined ISCC (
  echo.
  echo Compiling installer...
  "%ISCC%" installer\fun-tab.iss
  if errorlevel 1 (
    echo Packed the zip but the installer failed to compile.
    if not defined CI pause
    exit /b 1
  )
  echo Give people dist\FunTabSetup.exe — they run it, no unzip, no _internal folder to keep.
) else (
  echo.
  echo Give people dist\FunTab.zip
  echo They unzip it and double-click FunTab.exe - no Python, no pip.
  echo Keep FunTab.exe next to the _internal folder.
  echo Optional: install Inno Setup 6 and re-run to also get dist\FunTabSetup.exe.
)

exit /b 0
