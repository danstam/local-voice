$ErrorActionPreference = 'Stop'
$Project = Split-Path $PSScriptRoot -Parent
try {
    $Pointer = Join-Path $Project '.windows\active-runtime.txt'
    if (-not (Test-Path -LiteralPath $Pointer)) { throw 'Run ./setup.cmd first. No private runtime is installed.' }
    $Name = (Get-Content -LiteralPath $Pointer -Raw).Trim()
    if ($Name -notmatch '^[a-z0-9-]+$') { throw 'The private runtime selection is invalid. Rerun setup.' }
    $Python = Join-Path $Project ".windows\runtimes\$Name\python\python.exe"
    if (-not (Test-Path -LiteralPath $Python)) { throw 'The private runtime is incomplete. Rerun setup to repair it.' }
    if ($args -contains '--windowed') {
        $Windowed = Join-Path (Split-Path $Python -Parent) 'pythonw.exe'
        # Arguments are quoted for CreateProcess; no shell command is constructed.
        $Entry = Join-Path $Project 'windows_entry.py'
        Start-Process -FilePath $Windowed -ArgumentList @('-I', '-X', 'utf8', ('"' + $Entry + '"')) -WorkingDirectory $Project
        exit 0
    }
    & $Python -I -X utf8 -u (Join-Path $Project 'windows_entry.py') @args
    exit $LASTEXITCODE
} catch {
    Write-Host "Local Voice: $_" -ForegroundColor Red
    exit 1
}
