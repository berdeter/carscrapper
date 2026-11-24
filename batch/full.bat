@echo off
rem ===========================================================================
rem  full.bat : traitement COMPLET, du scraping jusqu'a l'estimation.
rem    scrape (toutes les pages) -> match -> details -> resolve -> estimation
rem  - les annonces non revues sont marquees inactives (--mark-missing)
rem  - les fiches orphelines sont retirees du cache final (--prune, avec sauvegarde)
rem  Arguments supplementaires transmis au pipeline, ex. :
rem    full.bat --scraper renew        (une seule source)
rem    full.bat --headless             (navigateur invisible)
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
echo === Pipeline complet (%STAMP%) ===
"%PY%" src\pipeline.py --max-pages 200 --mark-missing --prune %*
if errorlevel 1 goto :erreur
call "%~dp0_estimate.bat"
if errorlevel 1 goto :erreur
echo.
echo Termine : best_ev_opportunities.xlsx
exit /b 0
:erreur
echo.
echo [ERREUR] Pipeline interrompu (voir les messages ci-dessus).
exit /b 1
