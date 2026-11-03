@echo off
REM SmarterGamers Personal Clipper runner (Windows side of the n8n pipeline).
REM Uses the clipper venv (system Python 3.14 - the miniconda 3.11 venv crashes on ctranslate2 import).
set "VENV_PY=C:\SmarterGamers\clipper-venv314\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [runner] venv python not found at %VENV_PY%
    echo [runner] create it with: py -3.14 -m venv C:\SmarterGamers\clipper-venv314 ^&^& "%VENV_PY%" -m pip install -r requirements.txt faster-whisper "av<19" numpy
    exit /b 1
)
cd /d "%~dp0"
"%VENV_PY%" "%~dp0src\clipper\runner.py" %*
