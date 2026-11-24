@echo off
rem Etape finale commune : estimation -> best_ev_opportunities.xlsx / .csv
echo.
echo === Estimation (MarketEstimator) ===
"%PY%" src\market_estimator.py
if errorlevel 1 (
  echo [ERREUR] Estimation en echec. Le fichier best_ev_opportunities.xlsx est-il ouvert dans Excel ?
  exit /b 1
)
exit /b 0
