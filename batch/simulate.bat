@echo off
rem ===========================================================================
rem  simulate.bat : recalcule la simulation road trip 1000 km de toutes les
rem  variantes de models.json (apres modification du simulateur ou des donnees
rem  techniques), puis met a jour le cache final et l'estimation.
rem    simulation -> resolve -> estimation
rem  Pas de copie de sauvegarde : models.json est versionne (git diff models.json
rem  pour voir les changements, git checkout models.json pour annuler).
rem  Les voitures de reference relisent leur simulation dans models.json : rien
rem  a mettre a jour a la main.
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
"%PY%" src\algorithme_simulation_humaine.py models.json models.json
if errorlevel 1 goto :erreur
"%PY%" src\pipeline.py --only resolve %*
if errorlevel 1 goto :erreur
call "%~dp0_estimate.bat"
if errorlevel 1 goto :erreur
exit /b 0
:erreur
echo [ERREUR] Simulation interrompue. Pour annuler les changements de models.json : git checkout models.json
exit /b 1
