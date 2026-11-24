@echo off
rem ===========================================================================
rem  estimate.bat : relance UNIQUEMENT l'estimation sur resolved_cars.json
rem  (apres un changement des parametres de MarketEstimator ou des scores de
rem  marques). Aucun scraping, aucune resolution.
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
call "%~dp0_estimate.bat"
exit /b %errorlevel%
