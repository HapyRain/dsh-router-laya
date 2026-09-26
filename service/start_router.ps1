# Idempotent launcher for the dsh-router-laya judge service -- the package's Windows twin of
# service/start_router.sh. Same behaviour as the source repo's routing/start_router.ps1: skip
# when /health already answers, wait up to 60s for the model load, log the child to %TEMP%.
#
#   powershell -File service/start_router.ps1 [-Port 8765] [-Device cpu]
#
# Environment: LAYA_PYTHON, LAYA_MODEL, LAYA_DEVICE, LAYA_START_TIMEOUT (seconds, default 60).
# Resolution:
#   python: $env:LAYA_PYTHON -> <package>\.venv-router\Scripts\python.exe -> nearest .venv (dev) -> "python"
#   model:  $env:LAYA_MODEL -> <package>\weights\model -> nearest training\laya_router_finetuned (dev)
param(
    [int]$Port = 8765,
    [string]$Device = ""
)
$ErrorActionPreference = "Stop"
$pkg = Split-Path -Parent $PSScriptRoot
$script = Join-Path $pkg "service\laya_router.py"
$healthUrl = "http://127.0.0.1:$Port/health"
$timeoutSec = 60
if ($env:LAYA_START_TIMEOUT) { $timeoutSec = [int]$env:LAYA_START_TIMEOUT }
$outLog = Join-Path $env:TEMP "laya-router-service.out.log"
$errLog = Join-Path $env:TEMP "laya-router-service.err.log"

try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    if ($health.protocol -eq "finetuned") {
        Write-Host "[start_router] already running on :$Port (protocol=finetuned) -- skip"
        exit 0
    }
    Write-Host "[start_router] WARNING: something answers :$Port but protocol=$($health.protocol), not finetuned"
} catch {
    # not running -> start it below
}

function Find-Up([string]$marker) {
    # Walk up from $pkg looking for a relative marker path; returns its absolute path or $null.
    $dir = $pkg
    for ($i = 0; $i -lt 6 -and $dir; $i++) {
        $candidate = Join-Path $dir $marker
        if (Test-Path $candidate) { return $candidate }
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    return $null
}

# python
$py = $null
if ($env:LAYA_PYTHON -and (Test-Path $env:LAYA_PYTHON)) {
    $py = $env:LAYA_PYTHON
} else {
    $venvRouter = Join-Path $pkg ".venv-router\Scripts\python.exe"
    if (Test-Path $venvRouter) { $py = $venvRouter }
    else {
        $devVenv = Find-Up ".venv\Scripts\python.exe"   # dev checkout: the repo's own venv
        if ($devVenv) { $py = $devVenv } else { $py = "python" }
    }
}

# checkpoint
if ($env:LAYA_MODEL) {
    $model = $env:LAYA_MODEL
} else {
    $packaged = Join-Path $pkg "weights\model\model.safetensors"
    if (Test-Path $packaged) { $model = Join-Path $pkg "weights\model" }
    else { $model = Find-Up "training\laya_router_finetuned" }
}
if (-not $model) {
    Write-Host "[start_router] no checkpoint found: run 'node weights/fetch.mjs' to fill $pkg\weights\model," -ForegroundColor Red
    Write-Host "[start_router] or set LAYA_MODEL to an existing laya_router_finetuned directory." -ForegroundColor Red
    exit 2
}
if (-not (Test-Path $script)) {
    Write-Host "[start_router] service script missing: $script" -ForegroundColor Red
    exit 2
}

$env:PYTHONIOENCODING = "utf-8"
if ($Device -ne "") { $env:LAYA_DEVICE = $Device }

Write-Host "[start_router] launching service/laya_router.py --http on :$Port (model: $model)"
$proc = Start-Process -FilePath $py `
    -ArgumentList "`"$script`"","--http","--port",$Port `
    -WorkingDirectory $pkg -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog

# wait for /health (model load: seconds on GPU, ~70s on CPU)
$deadline = (Get-Date).AddSeconds($timeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.protocol -eq "finetuned") {
            Write-Host "[start_router] ready on :$Port (protocol=finetuned, pid=$($proc.Id))"
            exit 0
        }
    } catch {}
}
Write-Host "[start_router] service did not become healthy in ${timeoutSec}s -- see $errLog"
exit 1
