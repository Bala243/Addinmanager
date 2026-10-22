@echo off
title BIM Add-in Installer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-BIMAddinPackage.ps1" %*
echo.
pause
