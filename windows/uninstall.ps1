$ErrorActionPreference = 'Stop'
$Project = Split-Path $PSScriptRoot -Parent
$Private = Join-Path $Project '.windows'
Write-Host 'Close Local Voice before continuing. This removes only this copy of its private runtime.'
if ((Read-Host 'Type REMOVE to continue') -cne 'REMOVE') { exit 0 }
if ((Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith(($Private + '\'), [StringComparison]::OrdinalIgnoreCase) })) {
    throw 'Local Voice is still running. Close it, then retry. No unrelated Python processes were stopped.'
}
$Pointer = Join-Path $Private 'active-runtime.txt'
if (Test-Path -LiteralPath $Pointer) {
    $Name = (Get-Content -LiteralPath $Pointer -Raw).Trim()
    if ($Name -notmatch '^[a-z0-9-]+$') { throw 'Invalid runtime selection. Run setup to repair this installation first.' }
    $Python = Join-Path $Private "runtimes\$Name\python\python.exe"
    if (Test-Path -LiteralPath $Python) {
        & $Python -I -X utf8 -u (Join-Path $Project 'windows_entry.py') --setup --remove-command
        if ($LASTEXITCODE -ne 0) { throw 'Shortcut removal did not finish. The runtime was retained.' }
    }
}
if (Test-Path -LiteralPath $Private) { Remove-Item -LiteralPath $Private -Recurse -Force }
Write-Host 'Private runtime removed. Source files, model downloads, preferences, and existing system Python were retained.'
