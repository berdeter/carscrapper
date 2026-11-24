@echo off
rem ===========================================================================
rem  simulate.bat : recalcule la simulation road trip 1000 km de toutes les
rem  variantes de models.json (apres modification du simulateur ou des donnees
rem  techniques), puis met a jour le cache final et l'estimation.
rem    sauvegarde models.json -> simulation -> resolve -> estimation
rem  Penser ensuite a mettre a jour ref_sim_min dans market_estimator.py si les
rem  temps de la Dacia Spring ou de l'IONIQ 3 ont change (affiches ci-dessous).
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
if not exist archives mkdir archives
copy /y models.json "archives\models-%STAMP%.json" >nul
echo Sauvegarde : archives\models-%STAMP%.json
"%PY%" src\algorithme_simulation_humaine.py models.json models.json spring "ioniq 3"
if errorlevel 1 goto :erreur
"%PY%" src\pipeline.py --only resolve %*
if errorlevel 1 goto :erreur
call "%~dp0_estimate.bat"
if errorlevel 1 goto :erreur
exit /b 0
:erreur
echo [ERREUR] Simulation interrompue. models.json d'origine : archives\models-%STAMP%.json
exit /b 1
