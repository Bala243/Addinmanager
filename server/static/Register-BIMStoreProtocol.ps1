# One-time per-PC setup. Registers bimstore:// for the CURRENT USER (no admin needed).
$ErrorActionPreference = 'Stop'
$cfgSrc = Join-Path $PSScriptRoot 'config.json'
if (-not (Test-Path $cfgSrc)) { throw 'config.json missing. Download the setup bundle from the Store again.' }
$dest = Join-Path $env:LOCALAPPDATA 'BIMStore'
New-Item -ItemType Directory -Force $dest | Out-Null
foreach ($f in 'BIMStore-Handler.ps1','Install-BIMAddinPackage.ps1','config.json') { Copy-Item (Join-Path $PSScriptRoot $f) $dest -Force }
$cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" "%1"' -f (Join-Path $dest 'BIMStore-Handler.ps1')
$k = 'HKCU:\Software\Classes\bimstore'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name '(default)' -Value 'URL:BIM Store'
New-ItemProperty -Path $k -Name 'URL Protocol' -Value '' -Force | Out-Null
New-Item -Path "$k\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "$k\shell\open\command" -Name '(default)' -Value $cmd
$server = (Get-Content $cfgSrc -Raw | ConvertFrom-Json).serverUrl
Write-Host "Setup complete. Store: $server" -ForegroundColor Green
Write-Host 'Return to the Store in your browser and click "Install now".'
