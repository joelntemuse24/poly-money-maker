# Run Polydesk from Windows PowerShell (not from C:\WINDOWS\system32).
param(
    [Parameter(Position = 0)]
    [string]$Address
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $Address) {
    Write-Host "Usage: .\widget\run.ps1 0xYOUR_REAL_PROXY"
    Write-Host "Easier: double-click widget\index.html and paste the address there."
    exit 1
}

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    $Py = $VenvPy
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Py = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $Py = "py"
} else {
    Write-Host "Python not found. Double-click widget\index.html instead (no install needed)."
    exit 1
}

& $Py (Join-Path $Root "widget\polydesk.py") --address $Address
