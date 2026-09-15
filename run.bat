@echo off
setlocal
cd /d "%~dp0"

set "PYTHON="
py -3 -c "import sys; print(sys.executable)" >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)"') do set "PYTHON=%%I"
)
if not defined PYTHON (
  python -c "import sys; print(sys.executable)" >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)"') do set "PYTHON=%%I"
  )
)
if not defined PYTHON (
  echo Fun Tab needs Python 3.10 or newer.
  echo To make a standalone copy for other people, run build.bat on a machine that has Python.
  pause
  exit /b 1
)

"%PYTHON%" -c "import PIL, numpy" >nul 2>&1
if errorlevel 1 (
  echo Installing Fun Tab dependencies...
  "%PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install packages.
    echo To make a standalone copy for other people, run build.bat instead.
    pause
    exit /b 1
  )
)

set "PYTHONW=%PYTHON%"
for %%I in ("%PYTHON%") do set "PYDIR=%%~dpI"
if exist "%PYDIR%pythonw.exe" set "PYTHONW=%PYDIR%pythonw.exe"

start "" "%PYTHONW%" -m fun_tab
if errorlevel 1 (
  echo Could not start Fun Tab.
  pause
  exit /b 1
)
