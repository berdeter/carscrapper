@echo off
rem ---------------------------------------------------------------------------
rem  Environnement commun a tous les batchs (appele par "call _env.bat").
rem  - se place a la racine du projet (dossier parent de batch\)
rem  - choisit le Python du .venv s'il existe
rem  - force l'UTF-8 (accents et emojis dans la console)
rem ---------------------------------------------------------------------------
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo [ERREUR] Python introuvable : creer le .venv ou ajouter python au PATH.
  exit /b 1
)
for /f "delims=" %%i in ('call "%PY%" -c "import datetime; print(datetime.datetime.now().strftime('%%Y%%m%%d-%%H%%M%%S'))"') do set "STAMP=%%i"
exit /b 0
