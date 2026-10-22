@echo off
setlocal
cd /d "%~dp0"
rem Keep the SQLite database and uploaded packages next to this deployment.
rem Do not point BIMDEPLOY_HOME at a temporary folder if you need accounts to survive restarts.
set "BIMDEPLOY_HOME=%~dp0"
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate
python -m pip install -q -r requirements.txt
if not exist .env ( python manage.py init )
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue ^| Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } ^| Select-Object -First 1 -ExpandProperty IPAddress; if ($ip) { $ip }"`) do set "BIMDEPLOY_LAN_IP=%%I"
echo.
echo BIM Add-in Store data: %BIMDEPLOY_HOME%data\deployment.db
if defined BIMDEPLOY_LAN_IP (
  echo Open on this PC:     http://localhost:8000/
  echo Share on this Wi-Fi: http://%BIMDEPLOY_LAN_IP%:8000/
) else (
  echo Open on this PC: http://localhost:8000/
  echo No LAN IPv4 address was found. Connect to the required Wi-Fi or Ethernet network first.
)
echo Other PCs must be on the same trusted network and Windows Firewall must allow TCP 8000.
echo Run Configure-LAN-Access.ps1 once as Administrator to add that firewall rule.
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000
