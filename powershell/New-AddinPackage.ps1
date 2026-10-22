<#
Builds a Store-ready ZIP from your compiled add-in.   (run on the developer/admin PC)
  .\New-AddinPackage.ps1 -Id BIM.Exporter -Version 1.2.0 -Years 2024,2025 `
      -BuildDir .\bin\Release -AddinFile .\BIM.Exporter.addin -Assembly BIM.Exporter.dll `
      -Description "Exports model data" -DisplayName "BIM Exporter" [-Scope user|machine]
Output: .\dist\<Id>-<Version>.zip  (manifest.json at root, payload\* beneath, '/' separators).
Then upload it in the web portal (Administration > Publish) or with Publish-Package.ps1.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Id, [Parameter(Mandatory)][string]$Version,
  [Parameter(Mandatory)][string[]]$Years, [Parameter(Mandatory)][string]$BuildDir,
  [Parameter(Mandatory)][string]$AddinFile, [Parameter(Mandatory)][string]$Assembly,
  [string]$DisplayName = $Id, [string]$Description = '', [ValidateSet('user','machine')][string]$Scope = 'user',
  [string]$OutDir = '.\dist'
)
$ErrorActionPreference = 'Stop'
$Years = @($Years | ForEach-Object { "$_" -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$BuildDir = (Resolve-Path $BuildDir).Path; $AddinFile = (Resolve-Path $AddinFile).Path
if (-not (Test-Path (Join-Path $BuildDir $Assembly))) { throw "Assembly not found in BuildDir: $Assembly" }
[xml]$x = Get-Content $AddinFile -Raw
$node = $x.SelectSingleNode('/RevitAddIns/AddIn/Assembly'); if (-not $node) { throw '.addin has no <Assembly>.' }
$stage = Join-Path ([IO.Path]::GetTempPath()) ('pkg-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory $stage | Out-Null
$payload = Join-Path $stage 'payload'; New-Item -ItemType Directory $payload | Out-Null
Copy-Item (Join-Path $BuildDir '*') $payload -Recurse -Force
$node.InnerText = $Assembly   # relative; installer rewrites to the absolute installed path
$x.Save((Join-Path $payload ([IO.Path]::GetFileName($AddinFile))))
$manifest = [ordered]@{ id=$Id; version=$Version; displayName=$DisplayName; description=$Description; packageType='RevitAddin';
  supportedRevitVersions=@($Years); installScope=$Scope; updatePolicy='whenRevitClosed' }
$manifest | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $stage 'manifest.json') -Encoding UTF8
New-Item -ItemType Directory -Force $OutDir | Out-Null
$zipPath = Join-Path (Resolve-Path $OutDir).Path "$Id-$Version.zip"; if (Test-Path $zipPath) { Remove-Item $zipPath }
$script:zip = [IO.Compression.ZipFile]::Open($zipPath, 'Create')
function Add-Tree([string]$dir, [string]$prefix) {
  foreach ($i in Get-ChildItem -LiteralPath $dir -Force) {
    if ($i.PSIsContainer) { Add-Tree $i.FullName ($prefix + $i.Name + '/') }
    else { [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($script:zip, $i.FullName, ($prefix + $i.Name), 'Optimal') }
  }
}
try { Add-Tree $stage '' }
finally { $script:zip.Dispose(); Remove-Item -LiteralPath $stage -Recurse -Force }
Write-Host "Built $zipPath" -ForegroundColor Green
