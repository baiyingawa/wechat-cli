@echo off
setlocal
title wechatcli

set "DISTRO=%WECHAT_CLI_WSL_DISTRO%"
pushd "%~dp0"

if defined DISTRO (
    wsl.exe -d "%DISTRO%" -- bash -lc "exec ./start.sh"
) else (
    wsl.exe -- bash -lc "exec ./start.sh"
)
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
