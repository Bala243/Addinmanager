[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$PackagePath,
    # Install only for this Revit year (e.g. 2025). Default: every supported year detected on this PC.
    [Parameter(Mandatory = $false)]
    [string]$RevitYear,
    # Install for all manifest years even if that Revit year is not detected.
    [switch]$ForceAllYears,
    # Helper mode (used by the bimstore:// handler): download the package from the Store.
    [string]$Server,
    [string]$PackageId,
    [string]$Token
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-InstalledRevitYears {
    $years = @()
    foreach ($pf in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $pf) { continue }
        $root = Join-Path $pf 'Autodesk'
        if (Test-Path -LiteralPath $root) {
            Get-ChildItem -LiteralPath $root -Directory -Filter 'Revit 20*' -ErrorAction SilentlyContinue | ForEach-Object {
                if ($_.Name -match '^Revit (20\d{2})$' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'Revit.exe'))) { $years += $Matches[1] }
            }
        }
    }
    return @($years | Sort-Object -Unique)
}

function Get-SafeZipEntryPath([string]$EntryName) {
    $normal = $EntryName.Replace([string][char]92, '/')
    if ([string]::IsNullOrWhiteSpace($normal) -or $normal.StartsWith('/') -or $normal -match '^[A-Za-z]:') {
        throw "Unsafe ZIP path: $EntryName"
    }
    $parts = $normal.Split('/')
    if ($parts -contains '..') { throw "Unsafe ZIP path traversal: $EntryName" }
    return $normal
}

$script:downloadedZip = $null
if ($Server -and $PackageId -and $Token) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $tmpZip = Join-Path $env:TEMP ('bimstore-' + [Guid]::NewGuid().ToString('N') + '.zip')
    Write-Host "Downloading package from $Server ..."
    $resp = Invoke-WebRequest -Uri ("{0}/api/install/packages/{1}/download?token={2}" -f $Server.TrimEnd('/'), $PackageId, $Token) -OutFile $tmpZip -PassThru -UseBasicParsing
    $script:downloadedZip = $tmpZip
    $expected = [string]@($resp.Headers['X-Package-SHA256'])[0]
    $actual = (Get-FileHash -LiteralPath $tmpZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not $expected -or $actual -ne $expected.ToLowerInvariant()) { throw 'Downloaded package failed SHA-256 verification.' }
    Write-Host 'SHA-256 verified.'
    $PackagePath = $tmpZip
}
if (-not $PackagePath) {
    # One-click bundle: use the single ZIP placed next to this script (package.zip).
    $local = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.zip' -File -ErrorAction SilentlyContinue)
    if ($local.Count -eq 1) { $PackagePath = $local[0].FullName; Write-Host "Using package: $PackagePath" }
}
if (-not $PackagePath) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = 'Select the approved BIM Add-in Store package ZIP'
    $dialog.Filter = 'BIM Add-in package (*.zip)|*.zip'
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { Write-Host 'Cancelled.'; exit 0 }
    $PackagePath = $dialog.FileName
}

$PackagePath = (Resolve-Path -LiteralPath $PackagePath).Path
if ([IO.Path]::GetExtension($PackagePath) -ne '.zip') { throw 'Select a package ZIP file.' }
if (Get-Process -Name Revit -ErrorAction SilentlyContinue) {
    throw 'Close all running Revit sessions before installing or updating this add-in, then run the installer again.'
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($PackagePath)
$tempRoot = Join-Path $env:TEMP ('BIMAddinInstall-' + [Guid]::NewGuid().ToString('N'))
try {
    if ($zip.Entries.Count -gt 2000) { throw 'Package contains too many ZIP entries.' }
    $totalBytes = 0L
    foreach ($entry in $zip.Entries) {
        $null = Get-SafeZipEntryPath $entry.FullName
        $totalBytes += $entry.Length
        if ($totalBytes -gt 1GB) { throw 'Package expands beyond the 1 GB safety limit.' }
    }
    $zip.Dispose()
    [IO.Directory]::CreateDirectory($tempRoot) | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($PackagePath, $tempRoot)

    $manifestPath = Join-Path $tempRoot 'manifest.json'
    $payloadRoot = Join-Path $tempRoot 'payload'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Package is missing root manifest.json.' }
    if (-not (Test-Path -LiteralPath $payloadRoot -PathType Container)) { throw 'Package is missing payload/.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if (-not $manifest.id -or -not $manifest.version) { throw 'manifest.json must include id and version.' }
    $addonId = [string]$manifest.id
    if ($addonId -notmatch '^[A-Za-z0-9._-]{1,120}$') { throw 'Add-in ID contains unsupported characters.' }
    $years = @($manifest.supportedRevitVersions | ForEach-Object { [string]$_ } | Select-Object -Unique)
    if ($years.Count -eq 0) { throw 'manifest.json must list supportedRevitVersions, for example ["2025","2026"].' }
    foreach ($year in $years) { if ($year -notmatch '^20[0-9]{2}$') { throw "Invalid Revit version in manifest: $year" } }

    # Pick the correct Revit year folder(s): explicit -RevitYear > detected installed years > error.
    $detected = @(Get-InstalledRevitYears)
    if ($RevitYear) {
        if ($years -notcontains $RevitYear) { throw "Package supports Revit $($years -join ', '); not $RevitYear." }
        $years = @($RevitYear)
    } elseif (-not $ForceAllYears) {
        $match = @($years | Where-Object { $detected -contains $_ })
        if ($match.Count -eq 0) {
            throw "No supported Revit found. Package supports: $($years -join ', '). Detected on this PC: $(if ($detected) { $detected -join ', ' } else { 'none' }). Use -RevitYear <year> or -ForceAllYears to override."
        }
        $years = $match
    }
    Write-Host "Installing for Revit: $($years -join ', ')"

    $addinFiles = @(Get-ChildItem -LiteralPath $payloadRoot -Filter '*.addin' -File -Recurse)
    $dllFiles = @(Get-ChildItem -LiteralPath $payloadRoot -Filter '*.dll' -File -Recurse)
    if ($addinFiles.Count -eq 0) { throw 'payload/ must contain at least one .addin file.' }
    if ($dllFiles.Count -eq 0) { throw 'payload/ must contain at least one .dll file.' }

    $scope = [string]$manifest.installScope
    if ($scope -eq 'machine') {
        if (-not (Test-IsAdministrator)) { throw 'This package requires machine-wide installation. Right-click PowerShell and choose Run as administrator, then run the installer again. Alternatively, publish an approved per-user package with installScope="user".' }
        $baseRoot = Join-Path $env:ProgramData 'Autodesk\Revit\Addins'
    } else {
        $baseRoot = Join-Path $env:APPDATA 'Autodesk\Revit\Addins'
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    foreach ($year in $years) {
        $yearRoot = Join-Path $baseRoot $year
        $installRoot = Join-Path $yearRoot $addonId
        [IO.Directory]::CreateDirectory($yearRoot) | Out-Null
        $stageRoot = Join-Path $yearRoot ('.' + $addonId + '.staging-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stageRoot | Out-Null
        Get-ChildItem -LiteralPath $payloadRoot -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $stageRoot -Recurse -Force
        }

        # Rewrite each .addin Assembly value to the actual installed DLL path.
        foreach ($addin in Get-ChildItem -LiteralPath $stageRoot -Filter '*.addin' -File -Recurse) {
            [xml]$xml = Get-Content -LiteralPath $addin.FullName -Raw
            $assemblyNode = $xml.SelectSingleNode('/RevitAddIns/AddIn/Assembly')
            if (-not $assemblyNode -or [string]::IsNullOrWhiteSpace($assemblyNode.InnerText)) {
                throw "No Assembly element found in $($addin.Name)."
            }
            $assemblyLeaf = ($assemblyNode.InnerText -split '[\\/]+' | Select-Object -Last 1)
            $matchingDlls = @(Get-ChildItem -LiteralPath $stageRoot -Filter $assemblyLeaf -File -Recurse)
            if ($matchingDlls.Count -ne 1) { throw "Could not uniquely resolve Assembly DLL '$assemblyLeaf' for $($addin.Name). Found $($matchingDlls.Count)." }
            $assemblyNode.InnerText = $matchingDlls[0].FullName
            $xml.Save($addin.FullName)
        }

        if (Test-Path -LiteralPath $installRoot) {
            $backup = Join-Path $yearRoot ($addonId + '.backup-' + $stamp)
            Move-Item -LiteralPath $installRoot -Destination $backup
            Write-Host "Previous version backed up to: $backup"
        }
        Move-Item -LiteralPath $stageRoot -Destination $installRoot

        # Revit discovers .addin manifests directly in the version directory, not nested folders.
        foreach ($addin in Get-ChildItem -LiteralPath $installRoot -Filter '*.addin' -File -Recurse) {
            $destinationManifest = Join-Path $yearRoot $addin.Name
            if (Test-Path -LiteralPath $destinationManifest) {
                $existingXml = [xml](Get-Content -LiteralPath $destinationManifest -Raw)
                $existingAssembly = $existingXml.SelectSingleNode('/RevitAddIns/AddIn/Assembly')
                if ($existingAssembly -and $existingAssembly.InnerText -match [regex]::Escape($addonId)) {
                    Copy-Item -LiteralPath $destinationManifest -Destination ($destinationManifest + '.backup-' + $stamp) -Force
                } else {
                    throw "A different add-in already uses manifest name '$($addin.Name)' in $yearRoot. Resolve the name conflict before installing."
                }
            }
            $xml = [xml](Get-Content -LiteralPath $addin.FullName -Raw)
            $assemblyNode = $xml.SelectSingleNode('/RevitAddIns/AddIn/Assembly')
            # The Assembly value was rewritten to a staging absolute path; resolve its DLL name in final location.
            $dllLeaf = [IO.Path]::GetFileName($assemblyNode.InnerText)
            $dllCandidates = @(Get-ChildItem -LiteralPath $installRoot -Filter $dllLeaf -File -Recurse)
            if ($dllCandidates.Count -ne 1) { throw "Could not resolve installed DLL '$dllLeaf'." }
            $assemblyNode.InnerText = $dllCandidates[0].FullName
            $xml.Save($addin.FullName)
            Copy-Item -LiteralPath $addin.FullName -Destination $destinationManifest -Force
        }
        Write-Host "Installed $addonId v$($manifest.version) for Revit $year"
    }
    Write-Host ''
    Write-Host 'Installation completed. Start Revit and verify the add-in appears and loads correctly.' -ForegroundColor Green
}
finally {
    if ($zip) { try { $zip.Dispose() } catch {} }
    if ($script:downloadedZip -and (Test-Path -LiteralPath $script:downloadedZip)) { Remove-Item -LiteralPath $script:downloadedZip -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
