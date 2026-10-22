<#
Adds a narrowly scoped Windows Firewall rule so other devices on the trusted LAN
can open http://SERVER-LAN-IP:8000. Run once from an elevated PowerShell window.
It deliberately does not expose the server to public networks or the internet.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from PowerShell started with "Run as administrator".'
}

$name = "BIM Add-in Store LAN (TCP $Port)"
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $name -Description 'Allows BIM Add-in Store access from private local networks only.' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet | Out-Null

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Select-Object -ExpandProperty IPAddress

Write-Host "Firewall rule '$name' is active for Private networks only."
if ($ips) {
    $ips | ForEach-Object { Write-Host "Share this link with PCs on the same trusted network: http://$($_):$Port/" }
} else {
    Write-Warning 'No LAN IPv4 address found. Connect to Wi-Fi or Ethernet, then run ipconfig.'
}
