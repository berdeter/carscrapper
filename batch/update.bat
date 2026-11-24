@echo off
rem ===========================================================================
rem  update.bat : mise a jour RAPIDE (ex. quotidienne).
rem    scrape des premieres pages seulement (nouvelles annonces, prix modifies)
rem    -> match -> details (nouvelles fiches + fiches en echec a retenter)
rem    -> resolve -> estimation
rem  Pas de --mark-missing : un scraping partiel ne permet pas de savoir quelles
rem  annonces ont disparu (utiliser full.bat pour cela).
rem  Nombre de pages : 3 par defaut, ou "update.bat --max-pages 5".
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
echo === Mise a jour rapide (%STAMP%) ===
"%PY%" src\pipeline.py --max-pages 3 %*
if errorlevel 1 goto :erreur
call "%~dp0_estimate.bat"
if errorlevel 1 goto :erreur
exit /b 0
:erreur
echo [ERREUR] Mise a jour interrompue.
exit /b 1
