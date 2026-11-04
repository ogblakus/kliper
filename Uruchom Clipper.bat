@echo off
chcp 65001 >nul
title Clipper
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\bootstrap.ps1"
if errorlevel 1 pause
