@echo off
rem ===========================================================================
rem  reset-all.bat : repartir de zero apres une longue pause.
rem  1. ARCHIVE (deplace, ne supprime pas) les dossiers data\ (annonces, etapes
rem     du pipeline) et output\ (exports) dans archives\AAAAMMJJ-HHMMSS\
rem     Les voitures de reference (data\references.json) sont conservees.
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
for %%d in (data output) do (
  if exist "%%d" move "%%d" "%ARCH%\%%d" >nul && echo   archive : %%d\
)
if exist "%ARCH%\data\references.json" (
  mkdir data
  copy /y "%ARCH%\data\references.json" data\ >nul && echo   conserve : data\references.json
)
echo Donnees archivees dans %ARCH%
call "%~dp0full.bat"
exit /b %errorlevel%
