@echo off
rem Clipper Drop - drop videos or paste links; the local clipper (runner + n8n + Ollama) does the rest.
rem Uses the clipper venv; pythonw = no console window. Free/local only.
set "REPO=%~dp0.."
set "PY=C:\SmarterGamers\clipper-venv314\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"
cd /d "%REPO%\src"
start "" "%PY%" -m clipper_app.app
