$ErrorActionPreference = 'Stop'
$Project = Split-Path $PSScriptRoot -Parent
$Private = Join-Path $Project '.windows'
Write-Host 'Close Local Voice before continuing. This removes only this copy of its private runtime.'
if ((Read-Host 'Type REMOVE to continue') -cne 'REMOVE') { exit 0 }
if ((Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith(($Private + '\'), [StringComparison]::OrdinalIgnoreCase) })) {
    throw 'Local Voice is still running. Close it, then retry. No unrelated Python processes were stopped.'
}
if (Test-Path -LiteralPath $Private) { Remove-Item -LiteralPath $Private -Recurse -Force }
Write-Host 'Private runtime removed. Source files, model downloads, preferences, and existing system Python were retained.'
