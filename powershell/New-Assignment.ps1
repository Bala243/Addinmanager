[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$ServerUrl,
  [Parameter(Mandatory=$true)][string]$AdminToken,
  [Parameter(Mandatory=$true)][string]$AddonId,
  [Parameter(Mandatory=$true)][string]$Version,
  [string]$TargetGroup='*'
)
$body=@{addon_id=$AddonId;version=$Version;target_group=$TargetGroup} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri ($ServerUrl.TrimEnd('/')+'/api/assignments') -Headers @{'X-Admin-Token'=$AdminToken} -ContentType 'application/json' -Body $body | ConvertTo-Json -Depth 5
