@echo off
setlocal
cd /d "%~dp0wechat-link"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
    python wechat_link.py
) else (
    start "" pythonw.exe wechat_link.py
)
