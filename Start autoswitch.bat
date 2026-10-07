@echo off
rem Starts autoswitch.py. Double-click it while a download is running.
rem Anything you add after the name is passed on, e.g.  "Start autoswitch.bat" --below 10
setlocal
title autoswitch
cd /d "%~dp0"
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY for %%P in (
    "%USERPROFILE%\anaconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "%LOCALAPPDATA%\anaconda3\python.exe"
    "%LOCALAPPDATA%\miniconda3\python.exe"
    "%ProgramData%\anaconda3\python.exe"
    "%ProgramData%\miniconda3\python.exe"
    "C:\anaconda3\python.exe"
    "D:\anaconda3\python.exe"
) do if not defined PY if exist %%P set PY=%%P
if not defined PY goto nopython
%PY% "%~dp0autoswitch.py" %*
echo.
pause
exit /b

:nopython
echo Python was not found on this computer.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then run this again.
echo.
pause
exit /b 1
