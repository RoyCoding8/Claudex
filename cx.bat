@echo off
setlocal
cls
uv run --project "%~dp0." python "%~dp0cx.py" %*
exit /b %errorlevel%
