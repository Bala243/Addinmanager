# BIM Add-in Store — web self-service + authorized remote deployment

This upgrade builds on the existing FastAPI server and polling Windows agent. It adds a web portal, user accounts, device ownership, self-service installation on assigned workstations, administrator-authorized remote deployment, package publishing, and agent-reported deployment status.

## What works in this pilot

- Browser UI served by FastAPI at `/` (no separate frontend build tool required).
- Admin API-token protected package upload and account/device administration.
- First web administrator bootstrap, followed by username/password sign-in.
- Modeller accounts can request installs only for devices assigned to that account.
- Admin accounts can deploy to any enrolled workstation.
- Jobs are persisted in SQLite and consumed by the existing polling agent.
- Status in the UI is based on agent reports (`succeeded`, `failed`, `deferred`, `retry`), not a browser-side optimistic state.
- Agent package SHA-256 verification and ZIP/manifest validation remain in the Windows agent.

## Start the server (pilot)

1. Use Python 3.11+ on the server host. From `server`, create a virtual environment and install dependencies:

   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
   ```

2. Set unique secrets in the server process environment. Do not use the sample values:

   ```powershell
   $env:BIMDEPLOY_ADMIN_TOKEN = "<long-random-admin-api-secret>"
   $env:BIMDEPLOY_ENROLLMENT_TOKEN = "<long-random-enrollment-secret>"
   $env:BIMDEPLOY_DEVICE_TOKEN = "<long-random-device-secret>"
   ```

   These are initial server setup commands, not steps modellers perform every day. For a managed installation, configure them through your service manager or secret store.

3. Start the API (the provided launcher prints the exact LAN link to share):

   ```bat
   run-server.bat
   ```

4. Run `Configure-LAN-Access.ps1` **once as Administrator** to allow port 8000 only from the private local subnet. Then copy the link printed by the launcher—for example `http://192.168.1.25:8000/`—to another PC on the same Wi-Fi/Ethernet network. Do **not** copy `http://0.0.0.0:8000/` or `localhost`: neither identifies this server to another PC. Guest Wi-Fi/client isolation must be disabled by your network administrator. Do not expose this plain-HTTP setup to the internet or production users.

5. Select **First-time admin setup**. Enter the configured `BIMDEPLOY_ADMIN_TOKEN`, create the first admin username and a unique password of at least 12 characters. The bootstrap endpoint disables itself after the first user is created.

6. Select **Connection settings** on the login page and enter the same admin API token. This is kept in the browser's session storage for admin API operations. Then sign in with the web admin account.

7. Enroll the pilot Windows agent using the existing `agent/agent-config.example.json` configuration, with the same server URL, enrollment token and device token configured above. Run the agent using your existing approved test procedure.

8. In **Administration**, create modeller accounts and assign each registered device to its owner. Mark one assigned device as the primary/current-computer target. Modellers sign in and install only on their assigned devices. Admins may use **Deploy remotely** for any enrolled device.

## Package format

The ZIP should contain:

```text
manifest.json
payload/
  BIM.Exporter.addin
  BIM.Exporter.dll
  AnyDependency.dll
```

The manifest JSON submitted in the web form must match the ZIP root `manifest.json` on `id`, `version`, `packageType` and supported Revit versions. Keep the `.addin` assembly reference valid for the installed payload layout.

## Important behavior

- **Install here** means the primary/selected device assigned to the signed-in modeller. A normal browser cannot securely discover the workstation hostname by itself; the primary device is explicitly assigned by an administrator. If a user has multiple assigned devices, they can select the intended one in the target selector.
- **Deploy remotely** is available only to web accounts with the `admin` role.
- The agent must be running and able to reach the server. Jobs remain queued until the device checks in. If Revit is running, the existing agent reports `deferred` and retries later.
- A successful API request means “queued”, not “installed”. The dashboard only shows the final status after the agent reports it.
- Current pilot agent credentials use one shared device token. Use per-device credentials and revocation before production.

## Before production

This is an MVP upgrade, not a production security certification. Before rollout, add enterprise SSO/MFA, HTTPS with a trusted certificate, per-device credentials and revocation, package signing/publisher allow-lists, malware scanning, CSRF/origin hardening, rate limiting, durable database backups, structured audit retention, robust package validation, staged rollout/rollback, and service installation through your endpoint-management system. Run the supplied tests and a VM/second-device pilot first.


## Local download-and-install workflow (no always-running agent)

The Store now supports an authenticated **Download ZIP for local install** action. This downloads the approved ZIP to the modeller's computer; the browser itself cannot install files into Revit folders. The modeller then runs `installer/Install-BIMAddinPackage.ps1` (or downloads the same script from `/static/Install-BIMAddinPackage.ps1`) and selects the ZIP. The script validates the ZIP paths and package manifest, requires Revit to be closed, places the payload under the Revit Addins folder, rewrites each `.addin` Assembly path to the installed DLL, and backs up an existing add-in folder before replacing it.

- Per-user install target: `%APPDATA%\Autodesk\Revit\Addins\<year>`; normally does not require admin rights.
- Machine-wide install target: `%ProgramData%\Autodesk\Revit\Addins\<year>`; requires an elevated PowerShell session.
- For modellers without local admin rights, publish an approved manifest with `"installScope": "user"`.
- The installer handles supported Revit years declared in `supportedRevitVersions`. Verify the add-in against the real Revit versions and signed production DLLs before broad rollout.
- This local path does **not** provide remote installation or central real-time install reporting. For those capabilities, keep the managed Windows agent/endpoint-management mechanism.

On a Windows pilot computer, open PowerShell in the folder containing the script and run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\Install-BIMAddinPackage.ps1
```

The execution-policy command only affects that PowerShell process. Follow your organization's script-signing policy in production; do not broadly weaken machine execution policy. If the package uses `installScope: "machine"`, start PowerShell as Administrator first.

---
## v2 additions
Full runbook: `docs/STEP_BY_STEP.md`. New: `manage.py` (secret generation, admin creation, agent config), `.env` auto-load, admin web session replaces admin token in UI, auto-read manifest from ZIP, version history + Revit-year filter, one-click installer bundle (`Install.bat`), package revoke, user disable/password reset, audit log, login throttle, `powershell/New-AddinPackage.ps1`.
Quick start: `cd server && run-server.bat` -> `python manage.py create-admin` -> open the printed LAN link (or `http://localhost:8000` on the server PC). Accounts, sessions, packages, and jobs are stored in `server\data\deployment.db`; keep the `server\data` directory when updating or moving the deployment. On Render, configure `DATABASE_URL` from Render Postgres instead; the app stores accounts, jobs, and uploaded ZIP packages in Postgres so they survive web-service restarts.
