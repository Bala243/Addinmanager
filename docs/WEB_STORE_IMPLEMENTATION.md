# Web Store implementation notes

## Self-service vs remote deployment

- A modeller account can create install jobs only for workstation IDs assigned to that account in `user_devices`.
- The primary assigned workstation is the first/default target and is the portal's “Install here” target. Browsers cannot reliably identify a Windows hostname without a local bridge, so an administrator assigns the primary device instead of trusting a browser-provided hostname.
- Only a web account with role `admin` can request `target=remote` for any enrolled device.
- Every request is a persisted row in `jobs`; it is not considered installed until the existing Windows agent reports `succeeded`.
- Agent polling continues to use the existing endpoints and headers, preserving compatibility with `BIMDeploy-Agent.ps1`.

## New routes

- `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
- `POST /api/admin/bootstrap` (requires admin API token; first user only)
- `POST /api/admin/users` (requires admin API token)
- `POST /api/admin/device-owners` (requires admin API token)
- `GET /api/portal/catalog` (requires signed-in user)
- `POST /api/portal/deploy` (signed-in user; modeller only on assigned devices; remote target admin-only)
- Existing agent endpoints remain available: enroll, heartbeat, job poll, package download and job report.
- Existing package upload, group assignment, overview and health endpoints remain available.

## Security boundaries

This implementation is a tested pilot. User passwords are PBKDF2-HMAC-SHA256 hashed with unique random salts; web sessions are random bearer tokens stored as SHA-256 hashes with an eight-hour expiry. Admin API operations require a separate admin API token. Device ownership is checked server-side on every modeller install request. Uploaded ZIP paths, symlinks, root manifest metadata, expected payload files, and uncompressed size are checked before publication.

Still required before production: enterprise SSO/MFA, HTTPS, per-device credentials and revocation, signed package/publisher allow-list enforcement, CSRF/origin/rate-limit protections, central secrets management, audit retention, endpoint service hardening, deployment rollback/recovery tests, and security review. The existing agent's shared device token is a pilot limitation.


## Browser download vs. installation

`GET /api/portal/packages/{package_id}/download` requires a signed-in web session and returns the approved package ZIP. The portal includes a **Download ZIP for local install** action. `installer/Install-BIMAddinPackage.ps1` is a one-shot local installer, not a polling agent or Windows service. It extracts only after ZIP path checks, validates the manifest and payload, requires Revit to be closed, chooses `%APPDATA%` for `installScope=user` or `%ProgramData%` for `installScope=machine`, creates per-Revit-year folders, rewrites `.addin` Assembly paths, and backs up the previous add-in folder.

A browser download alone cannot write into the Revit Addins folder. The one-shot installer must be run locally. It cannot install remotely or provide reliable centralized status reporting; use the managed agent or an approved endpoint-management platform for those requirements.
