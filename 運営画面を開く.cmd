@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "TSUMIPLA_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%TSUMIPLA_PY%" (
  "%TSUMIPLA_PY%" tools\dictionary_ops.py serve
) else (
  py -3 tools\dictionary_ops.py serve
)
if errorlevel 1 pause
