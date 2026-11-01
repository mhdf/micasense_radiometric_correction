@echo off
rem Opens the radiometric correction UI keeping this console open, to see messages and errors
rem (the Python is the one of config\python_environment.json, see select_python.cmd)
cd /d "%~dp0.."
call "%~dp0_python.cmd"
if errorlevel 1 exit /b 1
"%PYTHON_EXE%" -X faulthandler -m radiometric_app window --root .
echo.
echo The UI was closed, exit code %ERRORLEVEL%.
pause
