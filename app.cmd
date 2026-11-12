@echo off
rem STEP Studio: opens the app window. Keep this console open while you work. Usage: app.cmd [part.stp]
call "%~dp0fc.cmd" app.py %*
