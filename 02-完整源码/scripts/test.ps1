$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $SourceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run setup_dev.ps1 first.' }
$env:PYTHONUTF8 = '1'
& $Python (Join-Path $SourceRoot 'scripts\run_tests.py')
if ($LASTEXITCODE -ne 0) { throw "Tests failed ($LASTEXITCODE)" }
