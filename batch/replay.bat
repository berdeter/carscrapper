@echo off
rem ===========================================================================
rem  replay.bat : rejeu HORS-LIGNE (aucun acces reseau) apres une modification
rem  de models.json (alias, variantes, puissances, mots_cles), des marques
rem  autorisees ou des regles de resolution.
rem    match (force) -> resolve (force) -> estimation
rem ===========================================================================
setlocal
call "%~dp0_env.bat"
if errorlevel 1 exit /b 1
echo === Rejeu hors-ligne match + resolve (%STAMP%) ===
"%PY%" src\pipeline.py --only match,resolve --force-match --force-resolve --prune %*
if errorlevel 1 goto :erreur
call "%~dp0_estimate.bat"
if errorlevel 1 goto :erreur
exit /b 0
:erreur
echo [ERREUR] Rejeu interrompu.
exit /b 1
