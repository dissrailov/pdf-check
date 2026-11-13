@echo off
rem Builds dist\PDFCheck.exe - a single file that runs without Python installed.
setlocal
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
    echo Python not found. Install it from https://www.python.org/downloads/
    pause
    exit /b 2
)

%PY% -m pip install --user --quiet pyinstaller pymupdf customtkinter tkinterdnd2
%PY% -m PyInstaller --noconfirm --onefile --windowed --name PDFCheck --collect-all tkinterdnd2 --collect-all customtkinter pdfcheck.pyw
echo.
echo Done: dist\PDFCheck.exe
pause
