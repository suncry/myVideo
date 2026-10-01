param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $SourceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run setup_dev.ps1 first.' }
$env:PYTHONUTF8 = '1'
function Invoke-Checked([string[]]$Arguments) {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Build step failed ($LASTEXITCODE)" }
}
Push-Location $SourceRoot
try {
    if (-not $SkipTests) { Invoke-Checked @('scripts\run_tests.py') }
    Invoke-Checked @('scripts\build_windows_icon.py')
    Invoke-Checked @('-m', 'PyInstaller', '--noconfirm', '--clean', 'YingKu-Windows.spec')
    $Executable = Join-Path $SourceRoot 'dist\影库\影库.exe'
    if (-not (Test-Path -LiteralPath $Executable)) { throw 'Windows executable was not produced.' }
    Write-Host "Build completed: $Executable" -ForegroundColor Green
} finally { Pop-Location }
