@echo off
title BIM Store one-time setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Register-BIMStoreProtocol.ps1"
pause
