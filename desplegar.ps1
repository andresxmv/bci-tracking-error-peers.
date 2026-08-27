# =====================================================================
#   DESPLEGAR A RAILWAY
#
#   Corre las pruebas, commitea, hace push a main y dispara el deploy.
#   Se detiene si algo falla, y pide confirmacion antes de tocar main.
#
#       .\desplegar.ps1
#       .\desplegar.ps1 -Mensaje "YTD exacto desde retornos diarios"
#       .\desplegar.ps1 -SaltarPruebas      # solo si ya las corriste
# =====================================================================
param(
    [string]$Mensaje = "YTD calendario exacto, semilla de retornos diarios y bloqueo de recarga de cuotas",
    [switch]$SaltarPruebas
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$ENVIRONMENT_ID = "5842466c-b54d-4984-adfd-ec75d769efaf"
$SERVICE_ID     = "a6d2baec-0c80-4abc-92fc-7f3f43d5fc9e"
$URL_PUBLICA    = "https://bci-tracking-error-final-production.up.railway.app/"

function Titulo($texto) {
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor DarkGray
    Write-Host "  $texto" -ForegroundColor Cyan
    Write-Host ("=" * 68) -ForegroundColor DarkGray
}

# --- Python del proyecto -------------------------------------------------
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "py" }

# --- 1. Pruebas ----------------------------------------------------------
if (-not $SaltarPruebas) {
    Titulo "1/5  Pruebas"
    & $python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`nLas pruebas fallaron. No se despliega." -ForegroundColor Red
        exit 1
    }
    Write-Host "Pruebas OK." -ForegroundColor Green
} else {
    Write-Host "Pruebas saltadas por parametro." -ForegroundColor Yellow
}

# --- 2. Que se va a subir ------------------------------------------------
Titulo "2/5  Cambios pendientes"
git status --short --branch
$pendientes = git status --porcelain
if (-not $pendientes) {
    Write-Host "No hay nada que commitear. Se desplegara el HEAD actual." -ForegroundColor Yellow
}

$rama = (git rev-parse --abbrev-ref HEAD).Trim()
Write-Host ""
Write-Host "Rama:    $rama"
Write-Host "Mensaje: $Mensaje"
Write-Host ""
$respuesta = Read-Host "Commitear, hacer push y desplegar? (s/N)"
if ($respuesta -notmatch '^[sSyY]') {
    Write-Host "Cancelado. No se toco nada." -ForegroundColor Yellow
    exit 0
}

# --- 3. Commit y push ----------------------------------------------------
if ($pendientes) {
    Titulo "3/5  Commit y push"
    git add -A
    git commit -m $Mensaje
    if ($LASTEXITCODE -ne 0) { Write-Host "Fallo el commit." -ForegroundColor Red; exit 1 }
} else {
    Titulo "3/5  Push"
}
git push origin $rama
if ($LASTEXITCODE -ne 0) { Write-Host "Fallo el push." -ForegroundColor Red; exit 1 }

$sha = (git rev-parse --short HEAD).Trim()
Write-Host "HEAD: $sha" -ForegroundColor Green

# --- 4. Deploy en Railway ------------------------------------------------
Titulo "4/5  Deploy en Railway ($sha)"
$mutacion = 'mutation Deploy($environmentId:String!,$serviceId:String!,$commitSha:String){serviceInstanceDeployV2(environmentId:$environmentId,serviceId:$serviceId,commitSha:$commitSha)}'
npx.cmd -y @railway/cli api $mutacion `
    --var environmentId=$ENVIRONMENT_ID `
    --var serviceId=$SERVICE_ID `
    --var commitSha=$sha --compact
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nRailway rechazo el deploy. Revisa que estes logueado: npx.cmd @railway/cli login" -ForegroundColor Red
    exit 1
}

# --- 5. Esperar a que responda ------------------------------------------
Titulo "5/5  Esperando a que la app responda"
Write-Host "El build tarda unos minutos. Ctrl+C si prefieres revisar despues.`n"
$listo = $false
foreach ($intento in 1..40) {
    Start-Sleep -Seconds 15
    try {
        $r = Invoke-WebRequest -Uri $URL_PUBLICA -TimeoutSec 20 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $listo = $true; break }
    } catch {
        Write-Host ("  intento {0}/40 - todavia no responde" -f $intento) -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($listo) {
    Write-Host "LISTO. La pagina responde." -ForegroundColor Green
} else {
    Write-Host "No respondio en 10 minutos. Revisa los logs en Railway." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  $URL_PUBLICA"
Write-Host "  ${URL_PUBLICA}?fondo=8640&peer_config=1&fecha_corte=2026-07-31"
Write-Host ""
Write-Host "Nota: /health va a seguir en 500 hasta que cargues una cartola" -ForegroundColor DarkGray
Write-Host "posterior al 25-08-2026. Es el data gate que ya existia." -ForegroundColor DarkGray
