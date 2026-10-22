# Web Store smoke test report

Executed locally with FastAPI TestClient against a temporary database and test-only secrets.

Passed:
- Web page serves successfully.
- First administrator bootstrap and username/password login.
- Device enrollment and modeller account creation.
- Device ownership assignment.
- Package upload with root manifest and payload checks.
- Modeller self-service deployment to an assigned device.
- Modeller remote deployment rejected with HTTP 403.
- Agent heartbeat, job polling and success report.
- Catalog reflects agent-reported `succeeded` status.
- Signed-in modeller can download the approved ZIP through the authenticated local-download endpoint.
- ZIP path traversal upload rejected with HTTP 400.
- Python syntax compilation and browser JavaScript syntax check.

Not tested here: execution of the PowerShell local installer on Windows, real Revit loading, signed production DLLs, Windows service deployment, multi-computer network routing, enterprise SSO, TLS configuration, rollback under filesystem locks, and production load/security testing. Use a VM and a second enrolled test computer before rollout.

Re-run: from the project root, `python tests/smoke_test.py`.
