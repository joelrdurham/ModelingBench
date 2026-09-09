@echo off
setlocal
echo This deletes every generated ModelingBench run, checkpoint, render, log, and research artifact.
set /p MODELBENCH_RESET_CONFIRM=Type DELETE-ALL-GENERATED to continue: 
if not "%MODELBENCH_RESET_CONFIRM%"=="DELETE-ALL-GENERATED" (
  echo Confirmation did not match. Nothing was deleted.
  exit /b 2
)
python -m modelbench reset --all --confirm DELETE-ALL-GENERATED
exit /b %ERRORLEVEL%

