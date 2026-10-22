# Test results and second-machine rollout guide

## What was tested in this review

The server Python source passed `py_compile`. The FastAPI application was then run in an isolated temporary directory with test-only tokens and a temporary SQLite database. These API checks passed:

- `/health` returns HTTP 200.
- Admin overview rejects a missing admin token (401) and accepts the test token (200).
- Device enrollment succeeds.
- Device heartbeat succeeds.
- Package upload succeeds for a generated test ZIP.
- Assignment to the `Pilot` group succeeds.
- The enrolled Pilot device receives one job.
- Package download succeeds and its SHA-256 matches the server-provided hash.
- Job status reporting succeeds.
- Admin overview shows the test device, package, and job.
- Invalid manifest ID is rejected (400).
- Invalid device token is rejected (401).
- Duplicate add-in version is rejected (409).

**Important limits:** the generated test DLL was dummy bytes. These checks do not prove that the PowerShell agent installs successfully, that the `.addin` file is accepted by Revit, or that a real add-in loads. The PowerShell script was not executed by Windows PowerShell in this environment. Run the Windows and Revit tests below before using it.

## Important packaging note

This cleaned source ZIP omits the bundled Python `.venv`, `__pycache__`, local SQLite database, and timestamped agent backup. Create a new virtual environment on the server machine instead. The original database in the uploaded ZIP had no device, package, assignment, job, or event records.

## Phase 1 — Test the server on Machine A

Use a disposable test machine or VM. Do not use the example package as an installable add-in; it contains no real DLL or `.addin` manifest.

1. Extract this ZIP to a stable path, for example `C:\BIMAddinDeployment`.
2. Open PowerShell in `C:\BIMAddinDeployment\server`.
3. Create and activate a fresh virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `py -3.11` is unavailable, install a supported Python version from your organization's approved source. Do not copy the virtual environment from another computer.

4. Set fresh test-only secrets in the same PowerShell window. Use different long random values for all three:

```powershell
$env:BIMDEPLOY_ADMIN_TOKEN = 'REPLACE-WITH-A-LONG-RANDOM-ADMIN-SECRET'
$env:BIMDEPLOY_ENROLLMENT_TOKEN = 'REPLACE-WITH-A-DIFFERENT-RANDOM-ENROLLMENT-SECRET'
$env:BIMDEPLOY_DEVICE_TOKEN = 'REPLACE-WITH-A-THIRD-RANDOM-DEVICE-SECRET'
```

5. Start the server bound to this computer only:

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Keep this window open. In another PowerShell window, verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Expected: `status` is `ok`.

6. Open `http://127.0.0.1:8000/` for the starter dashboard. Enter the admin token only into this local test dashboard. The dashboard is a basic MVP, not an enterprise portal.

## Phase 2 — Prepare a real test add-in package

Build or choose a harmless, approved test add-in that is known to work in the exact Revit release on the test endpoint.

The ZIP must have this structure at its root (not an extra enclosing folder):

```text
manifest.json
payload\
  YourTestAddin.dll
  YourTestAddin.addin
  DependencyA.dll   (only if required)
```

The root `manifest.json` must include matching `id`, `version`, `packageType: "RevitAddin"`, and `supportedRevitVersions`, for example:

```json
{
  "id": "Company.TestAddin",
  "version": "1.0.0",
  "packageType": "RevitAddin",
  "supportedRevitVersions": ["2025"],
  "installScope": "machine",
  "updatePolicy": "whenRevitClosed",
  "description": "Approved harmless test add-in"
}
```

Replace the example values with the real test add-in's values. The `Assembly` element in the `.addin` file should be a relative path to the DLL within the payload, for example `YourTestAddin.dll`. The current agent validates relative assembly paths and rewrites the registered manifest to the final installed DLL path.

Do not publish dummy DLL bytes or the repository's `ExampleAddin` documentation folder as a real package.

## Phase 3 — Publish and assign on Machine A

In a new PowerShell window, set `$root` to the extracted project folder and set the admin token to the same value used by the running server:

```powershell
$root = 'C:\BIMAddinDeployment'
$env:BIMDEPLOY_ADMIN_TOKEN = 'YOUR-ADMIN-TOKEN-FROM-THE-SERVER-WINDOW'
$server = 'http://127.0.0.1:8000'
```

Publish the package (update the paths to your actual files):

```powershell
& "$root\powershell\Publish-Package.ps1" `
  -ServerUrl $server `
  -AdminToken $env:BIMDEPLOY_ADMIN_TOKEN `
  -ZipPath 'C:\Builds\Company.TestAddin-1.0.0.zip' `
  -ManifestPath 'C:\Builds\manifest.json'
```

Create an assignment for the pilot group, not `*`:

```powershell
& "$root\powershell\New-Assignment.ps1" `
  -ServerUrl $server `
  -AdminToken $env:BIMDEPLOY_ADMIN_TOKEN `
  -AddonId 'Company.TestAddin' `
  -Version '1.0.0' `
  -TargetGroup 'Pilot'
```

The ZIP manifest and the manifest passed to `Publish-Package.ps1` must match. Do not use the wildcard target for the first deployment.

## Phase 4 — First agent test on Machine A

Do this before introducing Machine B, so you can isolate server/package problems from networking problems.

1. Copy `agent\agent-config.example.json` to `agent\agent-config.json`.
2. Set these values in the config:
   - `serverUrl`: `http://127.0.0.1:8000`
   - `deviceToken`: exactly the server's `BIMDEPLOY_DEVICE_TOKEN`
   - `enrollmentToken`: exactly the server's `BIMDEPLOY_ENROLLMENT_TOKEN`
   - `groups`: `["Pilot"]`
   - Keep the supplied `installRoot`, `statePath`, and `logPath` unless your test VM requires different locations.
3. Close Revit completely before installing.
4. Open an elevated PowerShell only if your test's machine-wide install paths require it. First inspect the script and paths; do not run unknown packages.
5. Run one cycle:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& "$root\agent\BIMDeploy-Agent.ps1" -ConfigPath "$root\agent\agent-config.json" -Once
```

6. Check the configured agent log (default: `C:\ProgramData\BIMAddinDeployment\agent.log`) and the server dashboard/overview.
7. Verify the job status and inspect the installed version folder under `C:\ProgramData\BIMAddinDeployment\Addins\<AddinId>\<Version>`.
8. Verify that the registered `.addin` file exists under `%ProgramData%\Autodesk\Revit\Addins\<year>` and that its `Assembly` path points to an existing DLL.
9. Launch the matching Revit release and verify the add-in loads and behaves correctly. Check Revit's add-in load warnings and logs if it does not.

A server job marked `succeeded` means the agent's installation checks completed; it is not proof that Revit successfully loaded the add-in.

## Phase 5 — Second-machine test

**Do not point Machine B at `127.0.0.1`.** On Machine B, that address means Machine B itself, not Machine A.

Before testing across computers, arrange an approved HTTPS endpoint for Machine A (for example, an organization-managed reverse proxy with a trusted TLS certificate). The project README explicitly warns against exposing the development Uvicorn server directly to the office network. The current MVP uses shared tokens, so plain HTTP would expose credentials and package traffic to network interception.

Once IT has configured the HTTPS endpoint:

1. Confirm Machine B can reach the approved URL in a browser or with `Invoke-RestMethod` and that the TLS certificate validates.
2. Install Python and run the FastAPI server only on Machine A using the organization's approved hosting/reverse-proxy configuration. Do not open port 8000 broadly to the office network.
3. On Machine B, copy only the agent folder (or deploy it using your endpoint-management tool) to a stable path such as `C:\ProgramData\BIMAddinDeployment\Agent`.
4. Create `agent-config.json` from the example on Machine B. Set `serverUrl` to the HTTPS URL, set the enrollment/device tokens securely, and set `groups` to `["Pilot"]`. Do not copy Machine A's `state.json`; each endpoint must enroll with its own identity.
5. Ensure the server's enrollment/device secrets match the config. Remember that the current MVP uses shared device credentials and does not yet provide per-device credential revocation.
6. Test connectivity from Machine B:

```powershell
Invoke-RestMethod 'https://YOUR-APPROVED-HTTPS-HOST/health'
```

7. Run the agent once from an elevated PowerShell if required:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& 'C:\ProgramData\BIMAddinDeployment\Agent\BIMDeploy-Agent.ps1' `
  -ConfigPath 'C:\ProgramData\BIMAddinDeployment\Agent\agent-config.json' -Once
```

8. Confirm Machine B appears in the server overview with group `Pilot`, confirm it receives only the Pilot assignment, and verify the installed files, `.addin` registration, agent log, and actual Revit loading.
9. Repeat with a harmless update version, with Revit open (should defer), and with an unsupported Revit version (should fail safely).

If HTTPS hosting, certificate validation, and a supported hosting arrangement are not yet available, stop after the same-machine test. Do not work around the issue by sending shared tokens over plain HTTP across the office network.

## Minimum test checklist

- [ ] Server health and admin authentication
- [ ] Device enrollment and heartbeat
- [ ] Package upload and duplicate-version rejection
- [ ] Pilot-only assignment
- [ ] Download SHA-256 matches
- [ ] Successful install on a disposable Windows VM
- [ ] `.addin` Assembly path resolves to the installed DLL
- [ ] Revit loads the real test add-in
- [ ] Revit-running update defers safely
- [ ] Unsupported Revit version is rejected
- [ ] Bad hash / malformed ZIP / missing DLL fail without leaving a partial installation
- [ ] Previous version and registration recover correctly after a simulated failure
- [ ] HTTPS certificate validates on the second machine

## Known gaps before production

This remains a pilot scaffold. It uses shared tokens, has a basic dashboard, and has not undergone a Windows PowerShell 5.1 execution test in this review. The package hash checks integrity against the server's value but does not establish publisher trust. Configure publisher signature verification, per-device credentials, HTTPS, least-privilege installation, tested rollback, audit/monitoring, and a managed service or endpoint-management deployment before broad rollout.
