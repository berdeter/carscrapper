@echo off
rem Etape finale commune : estimation -> output\best_ev_opportunities.xlsx / .csv
echo.
echo === Estimation (MarketEstimator) ===
"%PY%" src\market_estimator.py
if errorlevel 2 (
  echo [ERREUR] Choisir d'abord les voitures de reference : batch\set_reference_variant.bat
  exit /b 1
)
if errorlevel 1 (
  echo [ERREUR] Estimation en echec. Le fichier output\best_ev_opportunities.xlsx est-il ouvert dans Excel ?
  exit /b 1
)
exit /b 0
