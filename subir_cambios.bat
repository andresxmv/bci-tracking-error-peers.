@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ====================================================================
echo   SUBIR CAMBIOS A GITHUB  (Railway despliega solo con el push)
echo ====================================================================
echo.
echo Cambios pendientes:
git status --short
echo.
set /p RESP="Subir estos cambios? (s/N): "
if /i not "%RESP%"=="s" goto :cancelado
echo.
echo [1/3] git add
git add -A
if errorlevel 1 goto :error
echo [2/3] git commit
git commit -m "YTD calendario exacto, semilla de retornos diarios y bloqueo de recarga de cuotas"
if errorlevel 1 echo    (sin cambios nuevos que commitear, se continua)
echo [3/3] git push
git push origin main
if errorlevel 1 goto :error
echo.
echo ====================================================================
echo   LISTO. Railway ya esta construyendo.
echo   Avisale a Claude y el revisa el deploy.
echo ====================================================================
git log --oneline -1
echo.
pause
exit /b 0

:error
echo.
echo -------------------------------------------------------------------
echo   ALGO FALLO. Copia el mensaje de arriba y mandaselo a Claude.
echo -------------------------------------------------------------------
echo.
pause
exit /b 1

:cancelado
echo.
echo Cancelado. No se subio nada.
pause
exit /b 0
