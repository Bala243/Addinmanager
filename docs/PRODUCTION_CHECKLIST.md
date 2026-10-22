# Production readiness checklist

## Identity and access
- [ ] Replace shared device token with per-device credentials or mTLS certificates.
- [ ] Integrate admin portal with company identity provider and RBAC.
- [ ] Support device revocation and credential rotation.
- [ ] Separate package publisher, approver, and deployment operator roles.

## Package supply chain
- [ ] Sign packages and verify publisher signatures before install.
- [ ] Validate ZIP entries and reject path traversal, symlinks, unexpected files, and oversized expanded content.
- [ ] Keep immutable packages and signed manifests.
- [ ] Scan packages and dependencies using approved endpoint/security tools.

## Installer safety
- [ ] Use a dedicated, least-privilege installer service.
- [ ] Install to a versioned staging folder, validate, then switch registration.
- [ ] Keep previous version until smoke tests pass.
- [ ] Restore old files and `.addin` registration on failure.
- [ ] Verify all supported Revit releases and add-in types.
- [ ] Handle locked DLLs, reboot/logoff requirements, and concurrent Revit processes.

## Reliability
- [ ] Add job leases, timeouts, idempotency, retry backoff, and stale-job recovery.
- [ ] Report agent version and last-seen health.
- [ ] Add structured logs, metrics, alerting, and log retention.
- [ ] Back up database and package store; test restore.
- [ ] Add database migration process and supported upgrade path.

## Operations
- [ ] HTTPS only; restrict network ingress and egress.
- [ ] Use a supported production ASGI service behind a reverse proxy.
- [ ] Add package retention and disaster recovery policy.
- [ ] Pilot on representative devices and maintain a tested rollback release.
