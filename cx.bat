@echo off
setlocal
cls
set "CX_DIR=%~dp0"

set "CX_ARGS="
:parse
if "%~1"=="" goto run
set CX_ARGS=%CX_ARGS% "%~1"
shift
goto parse
:run
uv run --project "%CX_DIR:~0,-1%" python "%CX_DIR%cx.py" %CX_ARGS%

exit /b %errorlevel%
