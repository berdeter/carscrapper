@echo off
rem ===========================================================================
rem  set_reference_variant.bat : choisit la voiture NEUVE de reference d'un
rem  segment, a laquelle chaque annonce est comparee (indice d'opportunite).
rem    set_reference_variant.bat A dacia_spring_27kwh 17890
rem    set_reference_variant.bat B hyundai_ioniq_3_42kwh 31495 "remise 2000 deduite"
rem  Arguments : segment, variante_id (models.json), prix neuf reellement paye
rem  (remises deduites), remarque facultative.
rem  Sans argument : affiche les references actuelles et les variantes disponibles.
rem  Le choix est enregistre dans data\references.json (propre a chaque PC, non
rem  versionne). L'estimation est refusee tant qu'un segment n'a pas de reference.
rem  Ensuite : batch\estimate.bat pour recalculer.
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
if "%~1"=="" (
  "%PY%" src\references.py show
  exit /b 0
)
if "%~3"=="" (
  echo Usage : set_reference_variant.bat ^<segment^> ^<variante_id^> ^<prix_neuf^> [remarque]
  echo Sans argument : liste des variantes disponibles.
  exit /b 1
)
"%PY%" src\references.py set %*
exit /b %errorlevel%
