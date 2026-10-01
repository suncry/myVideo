@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_dev.ps1"
if errorlevel 1 (
  echo 操作失败，请查看上面的信息。
  pause
  exit /b 1
)
pause
