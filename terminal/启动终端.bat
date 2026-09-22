@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Haizhizi Terminal
echo.
echo  Starting terminal...
echo  --real : use real backend (run uvicorn backend.main:app first)
echo.
python console.py %*
if errorlevel 1 (
    echo.
    echo  Program exited abnormally, code=%errorlevel%
    pause
)
