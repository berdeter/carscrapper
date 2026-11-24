@echo off
rem ===========================================================================
rem  details-retry.bat : retente la lecture des fiches de detail en echec, y
rem  compris celles abandonnees apres 3 echecs, puis resolve + estimation.
rem  Avec "details-retry.bat --refresh-details" : relit TOUTES les fiches
rem  (utile une fois, les anciennes fiches ayant pu etre marquees lues a tort).
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
echo === Relecture des fiches de detail (%STAMP%) ===
"%PY%" src\pipeline.py --only details,resolve --retry-failed-details %*
if errorlevel 1 goto :erreur
call "%~dp0_estimate.bat"
if errorlevel 1 goto :erreur
exit /b 0
:erreur
echo [ERREUR] Relecture interrompue.
exit /b 1
