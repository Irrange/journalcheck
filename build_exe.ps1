$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root 'dist'
$launcher = Join-Path $root 'launcher\JournalCheckLauncher.cs'
$icon = Join-Path $root 'assets\journalcheck_icon.ico'
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'

if (-not (Test-Path $csc)) {
    throw 'csc.exe not found.'
}
if (-not (Test-Path $launcher)) {
    throw 'Launcher source not found.'
}
if (-not (Test-Path $icon)) {
    throw 'Application icon not found.'
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null
& $csc /nologo /target:winexe /out:"$dist\JournalCheck.exe" /win32icon:"$icon" /reference:System.Windows.Forms.dll /reference:System.dll /reference:System.Core.dll "$launcher"
if ($LASTEXITCODE -ne 0) {
    throw 'Launcher build failed.'
}
Write-Host "Built $dist\JournalCheck.exe"
