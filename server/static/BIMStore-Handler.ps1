# Invoked by Windows for bimstore://install?pkg=<guid>&token=<token>. Server URL comes ONLY from local config.json.
param([Parameter(Mandatory)][string]$Url)
$ErrorActionPreference = 'Stop'
try {
    if ($Url -notmatch '^bimstore://install\?pkg=([0-9a-fA-F-]{36})&token=([A-Za-z0-9_-]{20,100})/?$') { throw 'Invalid install link.' }
    $pkg = $Matches[1]; $token = $Matches[2]
    $cfg = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
    & (Join-Path $PSScriptRoot 'Install-BIMAddinPackage.ps1') -Server $cfg.serverUrl -PackageId $pkg -Token $token
} catch {
    Write-Host "INSTALL FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
Read-Host 'Press Enter to close'
