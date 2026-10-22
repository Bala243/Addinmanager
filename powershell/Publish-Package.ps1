[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$ServerUrl,
  [Parameter(Mandatory=$true)][string]$AdminToken,
  [Parameter(Mandatory=$true)][string]$ZipPath,
  [Parameter(Mandatory=$true)][string]$ManifestPath
)
$ErrorActionPreference='Stop'
if (!(Test-Path $ZipPath)) { throw "ZIP not found: $ZipPath" }
if (!(Test-Path $ManifestPath)) { throw "Manifest not found: $ManifestPath" }
$m=Get-Content $ManifestPath -Raw | ConvertFrom-Json
if (!$m.id -or !$m.version -or $m.packageType -ne 'RevitAddin') { throw 'Manifest requires id, version, packageType=RevitAddin.' }
$form=@{addon_id=[string]$m.id;version=[string]$m.version;manifest_json=(Get-Content $ManifestPath -Raw);file=Get-Item $ZipPath}
Invoke-RestMethod -Method Post -Uri ($ServerUrl.TrimEnd('/')+'/api/packages') -Headers @{'X-Admin-Token'=$AdminToken} -Form $form | ConvertTo-Json -Depth 5
