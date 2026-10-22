# BIM Add-in Deployment Manager endpoint agent - pilot scaffold.
# Run as a managed Windows service / scheduled task under an approved account.
# This script is a polling agent, not yet a hardened service. Test in a VM first.
[CmdletBinding()]
param([string]$ConfigPath = "$PSScriptRoot\agent-config.json", [switch]$Once)
$ErrorActionPreference = 'Stop'
if (!(Test-Path $ConfigPath)) { throw "Config not found: $ConfigPath. Copy agent-config.example.json to agent-config.json and configure it." }
$cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$base = $cfg.serverUrl.TrimEnd('/')
$headers = @{ 'X-Device-Token' = $cfg.deviceToken }
New-Item -ItemType Directory -Force -Path (Split-Path $cfg.statePath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $cfg.logPath) | Out-Null
function Log([string]$Message) { Add-Content -Path $cfg.logPath -Value "$(Get-Date -Format o) $Message" }
function Get-RevitVersions {
  $roots = @('HKLM:\SOFTWARE\Autodesk\Revit','HKLM:\SOFTWARE\WOW6432Node\Autodesk\Revit')
  $found = @()
  foreach ($root in $roots) { if (Test-Path $root) { Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object { if ($_.PSChildName -match '^Autodesk Revit (20\d{2})$') { $found += $Matches[1] } elseif ($_.PSChildName -match '^(20\d{2})$') { $found += $Matches[1] } } } }
  return @($found | Sort-Object -Unique)
}
function Get-MachineId {
  try { return (Get-CimInstance Win32_ComputerSystemProduct).UUID } catch { return $env:COMPUTERNAME }
}
function Send-Report([string]$JobId,[string]$Status,[string]$Message) {
  try { Invoke-RestMethod -Method Post -Uri "$base/api/jobs/$JobId/report" -Headers ($headers + @{ 'X-Device-Id' = $script:deviceId }) -ContentType 'application/json' -Body (@{status=$Status;message=$Message} | ConvertTo-Json -Compress) | Out-Null } catch { Log "Report failed for $JobId: $($_.Exception.Message)" }
}
function Invoke-Enroll {
  $body = @{hostname=$env:COMPUTERNAME;machine_id=(Get-MachineId);agent_version='0.1.0';revit_versions=(Get-RevitVersions);groups=@($cfg.groups);enrollment_token=$cfg.enrollmentToken} | ConvertTo-Json -Depth 5
  $r = Invoke-RestMethod -Method Post -Uri "$base/api/devices/enroll" -ContentType 'application/json' -Body $body
  $script:deviceId = $r.device_id
  Set-Content -Path $cfg.statePath -Value (@{deviceId=$script:deviceId} | ConvertTo-Json)
  Log "Enrolled device id $script:deviceId"
}
function Ensure-DeviceId {
  if (Test-Path $cfg.statePath) { try { $script:deviceId = (Get-Content $cfg.statePath -Raw | ConvertFrom-Json).deviceId } catch {} }
  if (!$script:deviceId) { Invoke-Enroll }
}
function Install-Package($job) {
  $jobId = [string]$job.job_id
  $temp = $null
  $target = $null
  $stage = $null
  $backupTarget = $null
  $registeredManifests = @()
  $committed = $false

  try {
    # Validate job identity before using values in filesystem paths.
    $addonId = [string]$job.addon_id
    $version = [string]$job.version
    if ($addonId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$') {
      throw "Invalid add-on ID: '$addonId'."
    }
    if ($version -notmatch '^[A-Za-z0-9][A-Za-z0-9.+_-]{0,49}$') {
      throw "Invalid add-on version: '$version'."
    }
    if ([string]::IsNullOrWhiteSpace([string]$job.download_url) -or
        [string]$job.download_url -notmatch '^/api/packages/[A-Za-z0-9-]+/download$') {
      throw 'Unexpected package download URL.'
    }
    if ([string]$job.sha256 -notmatch '^[A-Fa-f0-9]{64}$') {
      throw 'Server did not provide a valid SHA-256 hash.'
    }

    $supported = @($job.manifest.supportedRevitVersions | ForEach-Object { [string]$_ })
    if ($supported.Count -eq 0) { throw 'Package manifest has no supportedRevitVersions.' }
    foreach ($year in $supported) {
      if ($year -notmatch '^20\d{2}$') { throw "Invalid Revit version in manifest: '$year'." }
    }
    $installed = @(Get-RevitVersions)
    if (-not ($supported | Where-Object { $installed -contains $_ })) {
      Send-Report $jobId 'failed' "No supported Revit version found. Package supports: $($supported -join ', '); detected: $($installed -join ', ')"
      return
    }

    # Avoid replacing assemblies while Revit may have them loaded.
    if (Get-Process -Name Revit -ErrorAction SilentlyContinue) {
      Send-Report $jobId 'deferred' 'Revit is running; update will be retried after Revit closes.'
      return
    }

    $installRoot = [IO.Path]::GetFullPath([string]$cfg.installRoot)
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null

    $temp = Join-Path $env:TEMP ("BIMDeploy-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $temp | Out-Null
    $zip = Join-Path $temp 'package.zip'
    $expanded = Join-Path $temp 'expanded'
    $downloadUri = "$base$($job.download_url)"
    Invoke-WebRequest -Uri $downloadUri -Headers $headers -OutFile $zip

    # Verify the complete downloaded package before extraction.
    $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne ([string]$job.sha256).ToLowerInvariant()) {
      throw 'Package SHA-256 does not match the server hash.'
    }

    # Reject ZIP entries that could write outside the extraction directory.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
      foreach ($entry in $archive.Entries) {
        $entryName = $entry.FullName.Replace('\','/')
        if ($entryName.StartsWith('/') -or $entryName -match '^[A-Za-z]:' -or
            ($entryName -split '/') -contains '..') {
          throw "Unsafe path in package ZIP: '$entryName'."
        }
      }
    } finally {
      $archive.Dispose()
    }
    Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force

    $packageRoot = [IO.Path]::GetFullPath($expanded)
    $manifestFile = Join-Path $packageRoot 'manifest.json'
    if (!(Test-Path -LiteralPath $manifestFile -PathType Leaf)) {
      throw 'Package is missing manifest.json at its root.'
    }

    $manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
    if ($manifest.packageType -ne 'RevitAddin') { throw 'Unsupported packageType; expected RevitAddin.' }
    if ($manifest.id -ne $addonId -or $manifest.version -ne $version) {
      throw 'Package manifest ID/version does not match the assigned package.'
    }
    $manifestYears = @($manifest.supportedRevitVersions | ForEach-Object { [string]$_ })
    if ($manifestYears.Count -eq 0) { throw 'Package manifest has no supported Revit versions.' }
    foreach ($year in $manifestYears) {
      if ($year -notmatch '^20\d{2}$') { throw "Invalid Revit version in package manifest: '$year'." }
    }
    foreach ($year in $manifestYears) {
      if ($supported -notcontains $year) { throw 'Server manifest and package manifest have different supported Revit versions.' }
    }

    $payload = Join-Path $packageRoot 'payload'
    if (!(Test-Path -LiteralPath $payload -PathType Container)) {
      throw 'Package is missing payload directory.'
    }
    $payloadFiles = @(Get-ChildItem -LiteralPath $payload -Recurse -File)
    if ($payloadFiles.Count -eq 0) { throw 'Payload directory contains no files.' }

    # Require at least one valid .addin file and a valid Assembly element
    # that resolves to an existing file inside this payload.
    $addinFiles = @(Get-ChildItem -LiteralPath $payload -Filter '*.addin' -Recurse -File)
    if ($addinFiles.Count -eq 0) { throw 'Payload contains no .addin manifest.' }
    foreach ($addin in $addinFiles) {
      [xml]$xml = Get-Content -LiteralPath $addin.FullName -Raw
      $assemblyNode = $xml.SelectSingleNode('//Assembly')
      if (-not $assemblyNode -or [string]::IsNullOrWhiteSpace($assemblyNode.InnerText)) {
        throw "Add-in manifest '$($addin.Name)' is missing an Assembly element."
      }
      $assemblyPath = [string]$assemblyNode.InnerText
      if (-not [IO.Path]::IsPathRooted($assemblyPath)) {
        $assemblyPath = [IO.Path]::GetFullPath((Join-Path $addin.DirectoryName $assemblyPath))
      } else {
        $assemblyPath = [IO.Path]::GetFullPath($assemblyPath)
      }
      $payloadFull = [IO.Path]::GetFullPath($payload).TrimEnd('\') + '\'
      if (-not $assemblyPath.StartsWith($payloadFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Assembly path in '$($addin.Name)' points outside the payload directory."
      }
      if (!(Test-Path -LiteralPath $assemblyPath -PathType Leaf)) {
        throw "Assembly referenced by '$($addin.Name)' does not exist: '$assemblyPath'."
      }
    }

    # If an approved-publisher thumbprint allow-list is configured, require
    # every DLL in the payload to have a valid Authenticode signature from it.
    $allowedThumbprints = @()
    if ($cfg.PSObject.Properties.Name -contains 'approvedPublisherThumbprints') {
      $allowedThumbprints = @($cfg.approvedPublisherThumbprints | ForEach-Object {
        ([string]$_).Replace(' ','').ToUpperInvariant()
      })
    }
    $dllFiles = @($payloadFiles | Where-Object { $_.Extension -ieq '.dll' })
    if ($allowedThumbprints.Count -gt 0) {
      foreach ($dll in $dllFiles) {
        $sig = Get-AuthenticodeSignature -LiteralPath $dll.FullName
        $thumb = ''
        if ($sig.SignerCertificate) { $thumb = $sig.SignerCertificate.Thumbprint.Replace(' ','').ToUpperInvariant() }
        if ($sig.Status -ne 'Valid' -or $allowedThumbprints -notcontains $thumb) {
          throw "DLL signature is invalid or publisher is not approved: '$($dll.Name)'."
        }
      }
    } else {
      Log "WARNING: no approvedPublisherThumbprints configured; SHA-256 verifies package integrity, not publisher trust."
    }

    # Mark-of-the-Web removal is not a trust check. It is performed only after
    # hash, metadata, ZIP path, and .addin/assembly validation have passed.
    Get-ChildItem -LiteralPath $payload -Recurse -File | ForEach-Object {
      Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue
    }

    $addonRoot = Join-Path $installRoot $addonId
    $target = Join-Path $addonRoot $version
    $stage = Join-Path $addonRoot ('.stage-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -Path (Join-Path $payload '*') -Destination $stage -Recurse -Force

    # Validate relative Assembly paths in staging. Keep them relative until the
    # files are moved to their final version directory.
    $stagedAddins = @(Get-ChildItem -LiteralPath $stage -Filter '*.addin' -Recurse -File)
    if ($stagedAddins.Count -eq 0) { throw 'No .addin manifest was copied to staging.' }
    foreach ($addin in $stagedAddins) {
      [xml]$xml = Get-Content -LiteralPath $addin.FullName -Raw
      $assemblyNode = $xml.SelectSingleNode('//Assembly')
      if (-not $assemblyNode -or [string]::IsNullOrWhiteSpace($assemblyNode.InnerText)) {
        throw "Staged .addin '$($addin.Name)' has no Assembly element."
      }
      if ([IO.Path]::IsPathRooted($assemblyNode.InnerText)) {
        throw "Use a relative Assembly path in '$($addin.Name)' (for example, BIM.ExampleAddin.dll)."
      }
      $stagedAssembly = [IO.Path]::GetFullPath((Join-Path $addin.DirectoryName $assemblyNode.InnerText))
      $stagePrefix = [IO.Path]::GetFullPath($stage).TrimEnd('\') + '\'
      if (-not $stagedAssembly.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Assembly path in '$($addin.Name)' points outside the staged payload."
      }
      if (!(Test-Path -LiteralPath $stagedAssembly -PathType Leaf)) {
        throw "Staged assembly does not exist: '$stagedAssembly'."
      }
    }

    # Replace the version directory with rollback support.
    New-Item -ItemType Directory -Force -Path $addonRoot | Out-Null
    if (Test-Path -LiteralPath $target) {
      $backupTarget = Join-Path $addonRoot ('.previous-' + [guid]::NewGuid().ToString('N'))
      Move-Item -LiteralPath $target -Destination $backupTarget
    }
    Move-Item -LiteralPath $stage -Destination $target
    $stage = $null

    # Rewrite Assembly paths after the move so they point to the final install
    # directory, not the temporary staging folder which is deleted afterward.
    foreach ($addin in @(Get-ChildItem -LiteralPath $target -Filter '*.addin' -Recurse -File)) {
      [xml]$xml = Get-Content -LiteralPath $addin.FullName -Raw
      $assemblyNode = $xml.SelectSingleNode('//Assembly')
      if (-not $assemblyNode -or [string]::IsNullOrWhiteSpace($assemblyNode.InnerText)) {
        throw "Installed .addin '$($addin.Name)' has no Assembly element."
      }
      $assemblyNode.InnerText = [IO.Path]::GetFullPath((Join-Path $addin.DirectoryName $assemblyNode.InnerText))
      if (!(Test-Path -LiteralPath $assemblyNode.InnerText -PathType Leaf)) {
        throw "Installed assembly does not exist: '$($assemblyNode.InnerText)'."
      }
      $xml.Save($addin.FullName)
    }

    # Register one uniquely named .addin file per source manifest and year.
    # Existing registrations for this add-on are backed up for rollback.
    $registrationBackupDir = Join-Path $temp 'registration-backup'
    New-Item -ItemType Directory -Force -Path $registrationBackupDir | Out-Null
    foreach ($addin in @(Get-ChildItem -LiteralPath $target -Filter '*.addin' -Recurse -File)) {
      $safeManifestName = [IO.Path]::GetFileNameWithoutExtension($addin.Name)
      foreach ($year in $manifestYears) {
        $reg = Join-Path $env:ProgramData "Autodesk\Revit\Addins\$year"
        New-Item -ItemType Directory -Force -Path $reg | Out-Null
        $dest = Join-Path $reg ("BIMDeploy-" + $addonId + '-' + $safeManifestName + '.addin')
        if (Test-Path -LiteralPath $dest) {
          $backupFile = Join-Path $registrationBackupDir ($year + '-' + [IO.Path]::GetFileName($dest))
          Copy-Item -LiteralPath $dest -Destination $backupFile -Force
          $registeredManifests += [pscustomobject]@{ Destination=$dest; Backup=$backupFile }
        } else {
          $registeredManifests += [pscustomobject]@{ Destination=$dest; Backup=$null }
        }
        Copy-Item -LiteralPath $addin.FullName -Destination $dest -Force
      }
    }

    # Verify registered manifests and installed DLL paths before reporting success.
    foreach ($entry in $registeredManifests) {
      if (!(Test-Path -LiteralPath $entry.Destination -PathType Leaf)) {
        throw "Revit add-in registration was not created: '$($entry.Destination)'."
      }
      [xml]$registeredXml = Get-Content -LiteralPath $entry.Destination -Raw
      $registeredAssembly = $registeredXml.SelectSingleNode('//Assembly')
      if (-not $registeredAssembly -or !(Test-Path -LiteralPath $registeredAssembly.InnerText -PathType Leaf)) {
        throw "Registered .addin manifest references a missing assembly: '$($entry.Destination)'."
      }
    }

    $installedState = Join-Path $addonRoot 'installed.json'
    @{id=$addonId;version=$version;sha256=$job.sha256;installedAt=(Get-Date).ToString('o')} |
      ConvertTo-Json | Set-Content -LiteralPath $installedState -Encoding UTF8

    $committed = $true
    if ($backupTarget -and (Test-Path -LiteralPath $backupTarget)) {
      Remove-Item -LiteralPath $backupTarget -Recurse -Force -ErrorAction SilentlyContinue
    }
    Send-Report $jobId 'succeeded' "Installed $addonId $version"
    Log "Installed $addonId $version"
  } catch {
    $message = $_.Exception.Message
    # Best-effort rollback of registrations created/overwritten by this job.
    foreach ($entry in @($registeredManifests)) {
      try {
        if ($entry.Backup -and (Test-Path -LiteralPath $entry.Backup)) {
          Copy-Item -LiteralPath $entry.Backup -Destination $entry.Destination -Force
        } elseif (Test-Path -LiteralPath $entry.Destination) {
          Remove-Item -LiteralPath $entry.Destination -Force -ErrorAction SilentlyContinue
        }
      } catch { Log "Rollback of registration failed: $($_.Exception.Message)" }
    }
    if (-not $committed -and $target) {
      try {
        if ($backupTarget -and (Test-Path -LiteralPath $backupTarget)) {
          if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
          Move-Item -LiteralPath $backupTarget -Destination $target
        } elseif (Test-Path -LiteralPath $target) {
          # No previous version existed; remove the incomplete new version.
          Remove-Item -LiteralPath $target -Recurse -Force
        }
      } catch { Log "Rollback of installed files failed: $($_.Exception.Message)" }
    }
    Send-Report $jobId 'failed' $message
    Log "Install failed for $($job.addon_id) $($job.version): $message"
  } finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
      Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($temp -and (Test-Path -LiteralPath $temp)) {
      Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
}
function Invoke-Cycle {
  Ensure-DeviceId
  $hb = @{hostname=$env:COMPUTERNAME;agent_version='0.1.0';revit_versions=(Get-RevitVersions);groups=@($cfg.groups)} | ConvertTo-Json -Depth 5
  Invoke-RestMethod -Method Post -Uri "$base/api/devices/heartbeat" -Headers ($headers + @{ 'X-Device-Id' = $script:deviceId }) -ContentType 'application/json' -Body $hb | Out-Null
  $jobs = Invoke-RestMethod -Method Get -Uri "$base/api/devices/jobs" -Headers ($headers + @{ 'X-Device-Id' = $script:deviceId })
  foreach ($job in $jobs.jobs) { Install-Package $job }
}
do {
  try { Invoke-Cycle } catch { Log "Agent cycle failed: $($_.Exception.Message)" }
  if ($Once) { break }
  Start-Sleep -Seconds ([Math]::Max(30,[int]$cfg.pollSeconds))
} while ($true)
