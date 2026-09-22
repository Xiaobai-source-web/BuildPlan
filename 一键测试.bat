@echo off
chcp 65001 >nul
title 建策 BuildPlan · 一键测试
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem Python 解释器：优先本机 Anaconda，其次 PATH 里的 python
set "PYEXE=python"
if exist "%USERPROFILE%\Anaconda3\python.exe" set "PYEXE=%USERPROFILE%\Anaconda3\python.exe"
if exist "%USERPROFILE%\anaconda3\python.exe" set "PYEXE=%USERPROFILE%\anaconda3\python.exe"
if exist "%LOCALAPPDATA%\anaconda3\python.exe" set "PYEXE=%LOCALAPPDATA%\anaconda3\python.exe"

"%PYEXE%" "%~dp0一键测试.py"

if errorlevel 1 (
    echo.
    echo 运行结束或出错。按任意键关闭本窗口...
    pause >nul
)
