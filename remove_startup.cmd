@echo off
chcp 65001 >nul
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Server Backup Monitor.lnk"
if exist "%LINK%" del /f "%LINK%"
echo Автозапуск удалён. Данные не затронуты.
