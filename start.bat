@echo off
rem Double-click to open PDF Check. You can also drop PDF files or a folder onto this file.
setlocal

set PY=
set PYW=
where py >nul 2>nul && set PY=py -3&& set PYW=pyw -3
if not defined PY where python >nul 2>nul && set PY=python&& set PYW=pythonw
if not defined PY (
    echo Python not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer.
    pause
    exit /b 2
)

%PY% -c "import pymupdf, customtkinter, tkinterdnd2" >nul 2>nul
if errorlevel 1 (
    echo Installing components, one time only...
    %PY% -m pip install --user --quiet pymupdf customtkinter tkinterdnd2
)

start "" %PYW% "%~dp0pdfcheck.pyw" %*
