@echo off
setlocal
cd /d "%~dp0"

title ModelBench Reviewer
set "VENV_DIR=%CD%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "MODELBENCH_EXE=%VENV_DIR%\Scripts\modelbench.exe"

if not exist "%VENV_PYTHON%" (
    echo Setting up ModelBench for first use...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%VENV_DIR%"
    ) else (
        python -m venv "%VENV_DIR%"
    )
    if errorlevel 1 goto :failed
)

if not exist "%MODELBENCH_EXE%" (
    echo Installing ModelBench into the local environment...
    "%VENV_PYTHON%" -m pip install -e .
    if errorlevel 1 goto :failed
)

echo.
echo Starting ModelBench Reviewer...
echo Leave this window open while using the reviewer.
echo Press Ctrl+C here when you want to stop it.
echo.
"%MODELBENCH_EXE%" serve
if errorlevel 1 goto :failed
goto :eof

:failed
echo.
echo ModelBench could not be started. Review the error above.
pause
exit /b 1
