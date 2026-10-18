# The bootstrap leaves system Python alone; successful setup adds a user-only voice command.
$ErrorActionPreference = 'Stop'
$Project = Split-Path $PSScriptRoot -Parent
$Options = @($args)
$Private = Join-Path $Project '.windows'
$LogFolder = Join-Path $env:LOCALAPPDATA 'Local Voice'
New-Item -ItemType Directory -Force $LogFolder | Out-Null
$Log = Join-Path $LogFolder 'windows-bootstrap.log'
$Details = Join-Path $LogFolder 'windows-bootstrap-details.log'
Start-Transcript -Path $Log -Append | Out-Null
$Lock = $null
$FailureMessage = 'Setup could not start. Close any other Local Voice setup window, then run ./setup.cmd again.'
function Invoke-Quiet {
    param([string]$Program, [string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    & $Program @Arguments 2>&1 | Out-File -LiteralPath $Details -Append -Encoding utf8
    $script:NativeExit = $LASTEXITCODE
}
try {
    if ($Options -contains '--help') {
        Write-Host 'Local Voice setup: --cpu, --model small|turbo|medium|large-v3, --check, --prepare-only, --remove-command'
        exit 0
    }
    $Architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $Architecture = $env:PROCESSOR_ARCHITEW6432 }
    if ($Architecture -ne 'AMD64' -or [Environment]::OSVersion.Version.Major -lt 10) {
        $FailureMessage = 'This computer is not supported yet. Local Voice requires Windows 10 or 11 on a 64-bit Intel or AMD PC.'
        throw $FailureMessage
    }
    New-Item -ItemType Directory -Force $Private | Out-Null
    # The OS releases this lock even if the installer is killed or crashes.
    $Lock = [IO.File]::Open((Join-Path $Private 'setup.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    if (($Options -contains '--check') -or ($Options -contains '--remove-command')) {
        & (Join-Path $PSScriptRoot 'launch.ps1') --setup @Options
        exit $LASTEXITCODE
    }
    $Manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime.json') -Raw | ConvertFrom-Json
    $Archive = Join-Path $Private 'python-runtime.tar.gz'
    $Partial = "$Archive.part"
    $Valid = (Test-Path -LiteralPath $Archive) -and ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLower() -eq $Manifest.sha256)
    if (-not $Valid) {
        Write-Host 'Downloading files Local Voice needs... this may take a few minutes.'
        $FailureMessage = 'The download could not finish. Check your internet connection and free disk space, then run ./setup.cmd again.'
        if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { throw 'Windows curl.exe is missing. Install Windows updates and retry.' }
        $PartialValid = (Test-Path -LiteralPath $Partial) -and ((Get-FileHash -LiteralPath $Partial -Algorithm SHA256).Hash.ToLower() -eq $Manifest.sha256)
        if (-not $PartialValid) {
            Invoke-Quiet 'curl.exe' @('--silent', '--show-error', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https', '--retry', '3', '--retry-delay', '2', '--connect-timeout', '20', '--max-time', '900', '--continue-at', '-', '--output', $Partial, $Manifest.url)
            if ($NativeExit -in @(22, 33)) {
                Remove-Item -LiteralPath $Partial -Force -ErrorAction SilentlyContinue
                Invoke-Quiet 'curl.exe' @('--silent', '--show-error', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https', '--retry', '3', '--connect-timeout', '20', '--max-time', '900', '--output', $Partial, $Manifest.url)
            }
            if ($NativeExit -ne 0) { throw 'Python download failed. Run setup again to resume the partial download.' }
        }
        if ((Get-FileHash -LiteralPath $Partial -Algorithm SHA256).Hash.ToLower() -ne $Manifest.sha256) {
            Remove-Item -LiteralPath $Partial -Force
            throw 'Python archive checksum failed. No runtime was installed. Retry setup.'
        }
        Move-Item -LiteralPath $Partial -Destination $Archive -Force
    }
    if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) { throw 'Windows tar.exe is missing. Install Windows updates and retry.' }
    $FailureMessage = 'Setup could not unpack its files. Check free disk space and install Windows updates, then retry.'
    $Bootstrap = Join-Path $Private ('bootstrap-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $Bootstrap | Out-Null
    Invoke-Quiet 'tar.exe' @('-xzf', $Archive, '-C', $Bootstrap)
    if ($NativeExit -ne 0) { throw 'Could not extract the verified runtime.' }
    $Python = Join-Path $Bootstrap 'python\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { throw 'The verified archive did not contain the expected interpreter.' }
    try {
        $FailureMessage = 'Setup could not start its speech engine. See the log below for details.'
        & $Python -I -X utf8 -u (Join-Path $Project 'windows_entry.py') --setup @Options --bootstrap $Bootstrap
        $Result = $LASTEXITCODE
    } finally {
        # Only our new temporary bootstrap is removed, never an existing environment.
        Remove-Item -LiteralPath $Bootstrap -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($Result -ne 0) { exit $Result }
} catch {
    $_ | Out-File -LiteralPath $Details -Append -Encoding utf8
    Write-Host $FailureMessage -ForegroundColor Red
    Write-Host "Details saved to: $Details"
    exit 1
} finally {
    if ($Lock) { $Lock.Dispose() }
    Stop-Transcript | Out-Null
}
