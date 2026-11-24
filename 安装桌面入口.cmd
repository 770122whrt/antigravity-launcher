@echo off
setlocal
cd /d "%~dp0"
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 goto python311
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 goto python312
py -3.10 -c "import sys" >nul 2>&1
if not errorlevel 1 goto python310
python install.py
goto finished
:python311
py -3.11 install.py
goto finished
:python312
py -3.12 install.py
goto finished
:python310
py -3.10 install.py
:finished
if errorlevel 1 echo Setup did not complete. See the error above and README.md.
pause
