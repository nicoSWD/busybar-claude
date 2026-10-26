@echo off
rem Windows counterpart of busybarctl: runs busybar_status.py with the py launcher or python.
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%~dp0busybar_status.py" %*
  exit /b
)
python "%~dp0busybar_status.py" %*
exit /b
