@echo off
chcp 65001 >nul
title Server Backup Monitor
cd /d "%~dp0"

where py.exe >nul 2>&1
if errorlevel 1 goto fallback
py.exe -3 "%~dp0backup_monitor.py" --gui
exit /b
:fallback
set "PYTHON_EXE="
for /d %%D in ("%LocalAppData%\Programs\Python\Python*") do if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"
if not defined PYTHON_EXE goto path_python
"%PYTHON_EXE%" "%~dp0backup_monitor.py" --gui
exit /b
:path_python
where python.exe >nul 2>&1
if errorlevel 1 goto missing_python
python.exe "%~dp0backup_monitor.py" --gui
exit /b
:missing_python
echo Python 3 не найден. Установите Python с python.org.
pause
