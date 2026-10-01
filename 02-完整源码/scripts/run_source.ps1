$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $SourceRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $Pythonw)) { throw 'Run setup_dev.ps1 first.' }
$env:PYTHONUTF8 = '1'
Start-Process -FilePath $Pythonw -ArgumentList ('"' + (Join-Path $SourceRoot 'windows_entry.py') + '"') -WorkingDirectory $SourceRoot
