@echo off
chcp 65001 >nul
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Server Backup Monitor.lnk"
set "TARGET=%~dp0start_monitor.vbs"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($env:LINK); $s.TargetPath=$env:TARGET; $s.WorkingDirectory=Split-Path $env:TARGET; $s.WindowStyle=1; $s.Save()"
echo Автозапуск установлен: GUI откроется после входа в Windows.
