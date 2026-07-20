@echo off
setlocal
cls
set "CX_DIR=%~dp0"

uv run --project "%CX_DIR:~0,-1%" python "%CX_DIR%cx.py" %*

exit /b %errorlevel%