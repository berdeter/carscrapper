@echo off
rem ===========================================================================
rem  reset-all.bat : repartir de zero apres une longue pause.
rem  1. ARCHIVE (deplace, ne supprime pas) toutes les donnees d'annonces dans
rem     archives\AAAAMMJJ-HHMMSS\ : raw_listings, catalog_matches, resolution_log,
rem     llm_decisions, resolved_cars, exports best_ev_opportunities.*
rem     Les referentiels (models.json, classification_marques_*.json) sont gardes.
rem  2. lance full.bat (scraping complet -> estimation).
rem  Usage : reset-all.bat        (demande confirmation)
rem          reset-all.bat /y     (sans confirmation, ex. tache planifiee)
rem  Pour supprimer definitivement les anciennes donnees : effacer archives\.
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
if /i not "%~1"=="/y" (
  echo Toutes les annonces stockees vont etre archivees dans archives\%STAMP%\
  echo puis un scraping complet sera relance.
  choice /c ON /m "Continuer (O = oui, N = non)"
  if errorlevel 2 exit /b 0
)
set "ARCH=archives\%STAMP%"
mkdir "%ARCH%"
for %%f in (raw_listings.json catalog_matches.json resolution_log.json llm_decisions.json resolved_cars.json best_ev_opportunities.xlsx best_ev_opportunities.csv) do (
  if exist "%%f" move "%%f" "%ARCH%\" >nul && echo   archive : %%f
)
echo Donnees archivees dans %ARCH%
call "%~dp0full.bat"
exit /b %errorlevel%
