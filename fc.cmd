@echo off
rem Run a project script with FreeCAD's bundled Python - no PowerShell needed (use when .ps1 scripts are blocked).
rem   fc.cmd run_tests.py
rem   fc.cmd app.py C:\parts\part.stp
rem FreeCAD is searched in %LOCALAPPDATA%\Programs and %ProgramFiles%; override with FREECAD_PYTHON.
setlocal
set "PY="
if defined FREECAD_PYTHON if exist "%FREECAD_PYTHON%" set "PY=%FREECAD_PYTHON%"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\FreeCAD*") do if exist "%%D\bin\python.exe" set "PY=%%D\bin\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\FreeCAD*") do if exist "%%D\bin\python.exe" set "PY=%%D\bin\python.exe"
if not defined PY (
    echo FreeCAD not found. Install FreeCAD 1.x from https://www.freecad.org or set FREECAD_PYTHON to its bin\python.exe
    exit /b 1
)
if "%~1"=="" (
    echo FreeCAD python: %PY%
    echo usage: fc.cmd ^<script.py^> [arguments]    e.g.  fc.cmd run_tests.py
    exit /b 0
)
cd /d "%~dp0"
"%PY%" %*
exit /b %ERRORLEVEL%
