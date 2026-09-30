@echo off
setlocal

set "DISTRO=%WECHAT_CLI_WSL_DISTRO%"
pushd "%~dp0"
if defined DISTRO (
    wsl.exe -d "%DISTRO%" -- bash -lc "./stop-all.sh"
) else (
    wsl.exe -- bash -lc "./stop-all.sh"
)
popd

wsl.exe --shutdown
exit /b %ERRORLEVEL%
