@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%~dp0venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"

if defined PYTHON_EXE goto run_app

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0app.py"
    goto end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0app.py"
    goto end
)

echo [ERROR] No usable Python runtime was found.
echo [ERROR] Please install Python 3 or create .venv first.
pause
goto end

:run_app
"%PYTHON_EXE%" "%~dp0app.py"

:end
if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with an error.
    echo [HINT] Make sure dependencies from requirements.txt are installed.
    pause
)
endlocal
