param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $SourceRoot '.venv\Scripts\python.exe'
$DeliveryRoot = Split-Path -Parent $SourceRoot
$OfflineRoot = Join-Path $DeliveryRoot '05-离线开发环境'
$env:PYTHONUTF8 = '1'
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable" }
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (-not $PythonPath) {
        $Candidates = @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'), 'C:\Python312\python.exe')
        foreach ($Candidate in $Candidates) {
            if (Test-Path -LiteralPath $Candidate) { $PythonPath = $Candidate; break }
        }
    }
    if (-not $PythonPath) {
        $Launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($Launcher) {
            $Detected = & $Launcher.Source -3.12 -c 'import sys;print(sys.executable)'
            if ($LASTEXITCODE -eq 0) { $PythonPath = $Detected.Trim() }
        }
    }
    if (-not $PythonPath) {
        $Installer = Join-Path $OfflineRoot 'python-3.12.10-amd64.exe'
        if (-not (Test-Path -LiteralPath $Installer)) { throw 'Install 64-bit Python 3.12 first, or pass -PythonPath.' }
        $InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312'
        $Process = Start-Process -FilePath $Installer -ArgumentList @('/quiet', 'InstallAllUsers=0', ('TargetDir="' + $InstallRoot + '"'), 'Include_pip=1', 'Include_test=0', 'Include_launcher=0', 'Shortcuts=0', 'AssociateFiles=0', 'PrependPath=0') -Wait -PassThru
        if ($Process.ExitCode -ne 0) { throw "Python installation failed ($($Process.ExitCode))" }
        $PythonPath = Join-Path $InstallRoot 'python.exe'
    }
    Invoke-Checked $PythonPath @('-c', 'import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize(chr(80))==8')
    Invoke-Checked $PythonPath @('-m', 'venv', (Join-Path $SourceRoot '.venv'))
}
Invoke-Checked $VenvPython @('-c', 'import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize(chr(80))==8')
$Wheelhouse = Join-Path $OfflineRoot '依赖包'
if (Test-Path -LiteralPath $Wheelhouse) {
    Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--no-index', '--find-links', $Wheelhouse, '-r', (Join-Path $SourceRoot 'requirements-windows-lock.txt'))
} else {
    Invoke-Checked $VenvPython @('-m', 'pip', 'install', '-r', (Join-Path $SourceRoot 'requirements-windows-lock.txt'))
}
Write-Host 'Development environment is ready. Run source, test, or build next.' -ForegroundColor Green
