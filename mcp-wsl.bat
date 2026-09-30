@echo off
setlocal

pushd "%~dp0"
if defined WECHAT_CLI_WSL_DISTRO (
    wsl.exe -d "%WECHAT_CLI_WSL_DISTRO%" -- bash -lc "exec ./mcp.sh"
) else (
    wsl.exe -- bash -lc "exec ./mcp.sh"
)
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
