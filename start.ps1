# One-command setup + test + run (Windows PowerShell).
#   .\start.ps1          -> setup, run tests, start server
#   .\start.ps1 demo     -> setup, start server in background, run the demo, stop server
# If scripts are blocked:  Set-ExecutionPolicy -Scope Process Bypass
param([string]$Mode = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
    Write-Host ">> Tesseract not found (needed only for scanned images)."
    Write-Host "   Install: winget install UB-Mannheim.TesseractOCR  (then reopen PowerShell)"
}

if (-not (Test-Path .venv)) { Write-Host ">> Creating virtualenv"; python -m venv .venv }
. .\.venv\Scripts\Activate.ps1
Write-Host ">> Installing dependencies"
python -m pip install -q --upgrade pip
pip install -q -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

Write-Host ">> Running tests"
pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Mode -eq "demo") {
    Write-Host ">> Starting server in background"
    $server = Start-Process -PassThru -NoNewWindow python -ArgumentList "-m","uvicorn","app.main:app","--port","8000" -RedirectStandardOutput server.log -RedirectStandardError server.err.log
    try {
        for ($i = 0; $i -lt 30; $i++) {
            try { Invoke-RestMethod http://localhost:8000/health | Out-Null; break } catch { Start-Sleep -Milliseconds 500 }
        }
        $key = ((Get-Content .env | Select-String '^MEDX_API_KEY=') -split '=', 2)[1]
        python scripts\demo.py --url http://localhost:8000 --key $key
    } finally {
        Stop-Process -Id $server.Id -Force
    }
} else {
    Write-Host ">> Starting server on http://localhost:8000  (docs: http://localhost:8000/docs)"
    uvicorn app.main:app --reload --port 8000
}
