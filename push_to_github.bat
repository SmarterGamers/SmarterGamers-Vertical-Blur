@echo off
setlocal
cd /d "%~dp0"
echo =======================================================
echo   SmarterGamers Vertical Blur Editor - Push to GitHub
echo =======================================================
echo.
echo Current branch: main
echo Latest commit:
git log -1 --oneline
echo.
echo Pushing to https://github.com/SmarterGamers/SmarterGamers-Vertical-Blur ...
git push origin main
echo.
if errorlevel 1 (
    echo [ERROR] Git push failed. If Git Credential Manager prompted, please complete sign-in.
) else (
    echo [SUCCESS] GitHub is now up to date with the newest code!
)
echo =======================================================
pause
