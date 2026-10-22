"""BIM Add-in Store API: self-service installs + admin-authorized remote deployment.

Pilot implementation. Put behind HTTPS, use enterprise SSO, per-device credentials,
code-signing policy and security review before production.
"""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, sqlite3, time, uuid, zipfile, io, re
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

CODE_ROOT = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(CODE_ROOT / ".env")
ROOT = Path(os.getenv("BIMDEPLOY_HOME", CODE_ROOT))
DATA = ROOT / "data"
PKG_DIR = DATA / "packages"
DB_PATH = DATA / "deployment.db"
ADMIN_TOKEN = os.getenv("BIMDEPLOY_ADMIN_TOKEN", "CHANGE-ME-ADMIN-TOKEN")
ENROLLMENT_TOKEN = os.getenv("BIMDEPLOY_ENROLLMENT_TOKEN", "CHANGE-ME-ENROLLMENT-TOKEN")
DEVICE_TOKEN = os.getenv("BIMDEPLOY_DEVICE_TOKEN", "CHANGE-ME-DEVICE-TOKEN")
MAX_UPLOAD = int(os.getenv("BIMDEPLOY_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))
SESSION_TTL = 8 * 60 * 60
LOGIN_FAILS: dict[str, list[float]] = {}
STATIC_DIR = CODE_ROOT / "static"
DATA.mkdir(parents=True, exist_ok=True); PKG_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="BIM Add-in Store", version="1.0.0")
app.mount("/static", StaticFiles(directory=CODE_ROOT / "static"), name="static")


def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS devices(
          id TEXT PRIMARY KEY, hostname TEXT NOT NULL, machine_id TEXT NOT NULL UNIQUE,
          agent_version TEXT, revit_versions TEXT NOT NULL DEFAULT '[]', groups_json TEXT NOT NULL DEFAULT '[]',
          last_seen INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'online'
        );
        CREATE TABLE IF NOT EXISTS packages(
          id TEXT PRIMARY KEY, addon_id TEXT NOT NULL, version TEXT NOT NULL, package_file TEXT NOT NULL,
          sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at INTEGER NOT NULL,
          UNIQUE(addon_id, version)
        );
        CREATE TABLE IF NOT EXISTS assignments(
          id TEXT PRIMARY KEY, addon_id TEXT NOT NULL, version TEXT NOT NULL, target_group TEXT NOT NULL DEFAULT '*',
          created_at INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, device_id TEXT NOT NULL, package_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, message TEXT NOT NULL DEFAULT '',
          UNIQUE(device_id, package_id)
        );
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, device_id TEXT, event_type TEXT NOT NULL, details TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users(
          username TEXT PRIMARY KEY, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'modeller', enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_devices(
          username TEXT NOT NULL, device_id TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(username, device_id), FOREIGN KEY(username) REFERENCES users(username), FOREIGN KEY(device_id) REFERENCES devices(id)
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at INTEGER NOT NULL,
          FOREIGN KEY(username) REFERENCES users(username)
        );
        ''')
        cols = {r[1] for r in c.execute("PRAGMA table_info(packages)")}
        if "revoked" not in cols: c.execute("ALTER TABLE packages ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
        if "release_notes" not in cols: c.execute("ALTER TABLE packages ADD COLUMN release_notes TEXT NOT NULL DEFAULT ''")
        if "uploaded_by" not in cols: c.execute("ALTER TABLE packages ADD COLUMN uploaded_by TEXT NOT NULL DEFAULT ''")
        c.execute("CREATE TABLE IF NOT EXISTS install_tokens(token_hash TEXT PRIMARY KEY, package_id TEXT NOT NULL, username TEXT NOT NULL, expires_at INTEGER NOT NULL)")
        c.execute("DELETE FROM install_tokens WHERE expires_at < ?", (int(time.time()),))
        # expire stale sessions and mark old devices offline
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        c.execute("UPDATE devices SET status='offline' WHERE last_seen < ?", (int(time.time()) - 600,))

init_db()


WEAK_MARKERS = ("change-me", "replace-with", "example")


def token_ok(provided: str | None, expected: str) -> bool:
    if not (provided and expected) or len(expected) < 24:
        return False
    if any(m in expected.lower() for m in WEAK_MARKERS):
        return False
    return secrets.compare_digest(provided, expected)


def _session_user(raw: str) -> Optional[dict]:
    key = hashlib.sha256(raw.encode()).hexdigest()
    with db() as c:
        row = c.execute('''SELECT u.username,u.role,u.enabled,s.expires_at FROM sessions s
          JOIN users u ON u.username=s.username WHERE s.token_hash=?''', (key,)).fetchone()
    if not row or not row["enabled"] or row["expires_at"] < int(time.time()):
        return None
    return dict(row)


def require_admin(x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)) -> str:
    """Admin = valid X-Admin-Token (scripts/bootstrap) OR signed-in web user with role admin."""
    if token_ok(x_admin_token, ADMIN_TOKEN):
        return "admin-token"
    if authorization and authorization.lower().startswith("bearer "):
        u = _session_user(authorization[7:].strip())
        if u and u["role"] == "admin":
            return u["username"]
    raise HTTPException(401, "Admin authentication required (admin login or X-Admin-Token).")


def require_device(x_device_token: Optional[str] = Header(default=None)):
    if not token_ok(x_device_token, DEVICE_TOKEN):
        raise HTTPException(401, "Invalid device credential. Configure BIMDEPLOY_DEVICE_TOKEN.")


def password_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if len(password) < 12:
        raise HTTPException(400, "Password must be at least 12 characters.")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return salt.hex(), digest.hex()


def current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please sign in.")
    raw = authorization[7:].strip()
    key = hashlib.sha256(raw.encode()).hexdigest()
    with db() as c:
        row = c.execute('''SELECT u.username,u.role,u.enabled,s.expires_at FROM sessions s
          JOIN users u ON u.username=s.username WHERE s.token_hash=?''', (key,)).fetchone()
    if not row or not row["enabled"] or row["expires_at"] < int(time.time()):
        raise HTTPException(401, "Session expired. Please sign in again.")
    return dict(row)


def require_admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator role required.")
    return user


class EnrollRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    machine_id: str = Field(min_length=8, max_length=255)
    agent_version: str = "1.0.0"
    revit_versions: list[str] = []
    groups: list[str] = []
    enrollment_token: str
class HeartbeatRequest(BaseModel):
    hostname: str
    agent_version: str = "1.0.0"
    revit_versions: list[str] = []
    groups: list[str] = []
class JobReport(BaseModel):
    status: str
    message: str = ""
class LoginRequest(BaseModel):
    username: str
    password: str
class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: str = "modeller"
class OwnershipRequest(BaseModel):
    username: str
    device_id: str
    is_primary: bool = True
class AssignmentRequest(BaseModel):
    addon_id: str
    version: str
    target_group: str = "*"
class DeployRequest(BaseModel):
    addon_id: str
    version: str
    device_id: Optional[str] = None
    target: str = "current"  # current = user's primary assigned device; remote = admin-only


@app.get("/health")
def health():
    # Do not include secrets here.  The storage path is useful when diagnosing
    # accidental use of a temporary/different deployment directory.
    return {"status": "ok", "service": "BIM Add-in Store", "time": int(time.time()), "storage": str(DATA)}

@app.get("/", response_class=HTMLResponse)
def home():
    return (CODE_ROOT / "static" / "index.html").read_text(encoding="utf-8")

@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    tkey = f"{request.client.host if request.client else '?'}|{req.username.lower()}"
    now_f = time.time()
    LOGIN_FAILS[tkey] = [t for t in LOGIN_FAILS.get(tkey, []) if now_f - t < 900]
    if len(LOGIN_FAILS[tkey]) >= 5:
        raise HTTPException(429, "Too many failed sign-ins. Try again in 15 minutes.")
    with db() as c:
        row = c.execute("SELECT username,password_salt,password_hash,role,enabled FROM users WHERE username=?", (req.username,)).fetchone()
    if not row or not row["enabled"]:
        LOGIN_FAILS[tkey].append(now_f)
        raise HTTPException(401, "Invalid username or password.")
    _, computed = password_hash_for_check(req.password, bytes.fromhex(row["password_salt"]))
    if not hmac.compare_digest(computed, row["password_hash"]):
        LOGIN_FAILS[tkey].append(now_f)
        raise HTTPException(401, "Invalid username or password.")
    LOGIN_FAILS.pop(tkey, None)
    raw = secrets.token_urlsafe(40); key = hashlib.sha256(raw.encode()).hexdigest(); expiry = int(time.time()) + SESSION_TTL
    with db() as c:
        c.execute("INSERT INTO sessions(token_hash,username,expires_at) VALUES(?,?,?)", (key, req.username, expiry))
        c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)", (int(time.time()), "user_login", json.dumps({"username": req.username})))
    return {"access_token": raw, "token_type": "bearer", "expires_at": expiry, "username": row["username"], "role": row["role"]}

def password_hash_for_check(password: str, salt: bytes) -> tuple[str, str]:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return salt.hex(), digest.hex()

@app.post("/api/auth/logout")
def logout(user: dict = Depends(current_user), authorization: Optional[str] = Header(default=None)):
    raw = authorization[7:].strip(); key = hashlib.sha256(raw.encode()).hexdigest()
    with db() as c: c.execute("DELETE FROM sessions WHERE token_hash=?", (key,))
    return {"status": "signed_out"}

@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "role": user["role"]}

@app.post("/api/devices/enroll")
def enroll(req: EnrollRequest):
    if not token_ok(req.enrollment_token, ENROLLMENT_TOKEN):
        raise HTTPException(401, "Invalid enrollment token or server not configured.")
    device_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, req.machine_id.lower())); now = int(time.time())
    with db() as c:
        c.execute('''INSERT INTO devices(id,hostname,machine_id,agent_version,revit_versions,groups_json,last_seen,status)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(machine_id) DO UPDATE SET hostname=excluded.hostname,
          agent_version=excluded.agent_version,revit_versions=excluded.revit_versions,groups_json=excluded.groups_json,
          last_seen=excluded.last_seen,status='online' ''',
          (device_id, req.hostname, req.machine_id, req.agent_version, json.dumps(req.revit_versions), json.dumps(req.groups), now, "online"))
        c.execute("INSERT INTO events(ts,device_id,event_type,details) VALUES(?,?,?,?)", (now, device_id, "enrolled", "{}"))
    return {"device_id": device_id, "status": "enrolled"}

@app.post("/api/devices/heartbeat")
def heartbeat(req: HeartbeatRequest, x_device_id: str = Header(...), _: None = Depends(require_device)):
    now = int(time.time())
    with db() as c:
        row = c.execute("SELECT id FROM devices WHERE id=?", (x_device_id,)).fetchone()
        if not row: raise HTTPException(404, "Device is not enrolled")
        c.execute("UPDATE devices SET hostname=?,agent_version=?,revit_versions=?,groups_json=?,last_seen=?,status='online' WHERE id=?",
                   (req.hostname, req.agent_version, json.dumps(req.revit_versions), json.dumps(req.groups), now, x_device_id))
        dev = c.execute("SELECT groups_json FROM devices WHERE id=?", (x_device_id,)).fetchone(); groups = set(json.loads(dev["groups_json"] or "[]"))
        for a in c.execute("SELECT * FROM assignments WHERE enabled=1").fetchall():
            if a["target_group"] == "*" or a["target_group"] in groups:
                p = c.execute("SELECT id FROM packages WHERE addon_id=? AND version=? AND revoked=0", (a["addon_id"], a["version"])).fetchone()
                if p: queue_job(c, x_device_id, p["id"], now)
    return {"status": "ok", "server_time": now}

def queue_job(c, device_id: str, package_id: str, now: int) -> str:
    existing = c.execute("SELECT id,status FROM jobs WHERE device_id=? AND package_id=?", (device_id, package_id)).fetchone()
    if existing:
        if existing["status"] in ("succeeded", "uninstalled"):
            c.execute("UPDATE jobs SET status='queued',updated_at=?,message='Queued by new request' WHERE id=?", (now, existing["id"]))
        return existing["id"]
    jid = str(uuid.uuid4())
    c.execute("INSERT INTO jobs(id,device_id,package_id,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)", (jid, device_id, package_id, now, now))
    return jid

@app.post("/api/packages")
async def upload_package(admin: str = Depends(require_admin), file: UploadFile = File(...),
                         addon_id: Optional[str] = Form(default=None), version: Optional[str] = Form(default=None),
                         manifest_json: Optional[str] = Form(default=None), release_notes: str = Form(default="")):
    """Publish a package. manifest.json inside the ZIP is the source of truth; form fields are optional cross-checks."""
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD: raise HTTPException(413, "Package exceeds upload limit")
    if not content.startswith(b"PK\x03\x04"): raise HTTPException(400, "Upload must be a ZIP package")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            entries = zf.infolist(); names = []
            if not entries or len(entries) > 10000: raise HTTPException(400, "ZIP is empty or has too many entries")
            total = 0
            for entry in entries:
                name = entry.filename.replace("\\", "/"); parts = name.split("/"); mode = (entry.external_attr >> 16) & 0xFFFF
                if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts or "\x00" in name:
                    raise HTTPException(400, f"Unsafe ZIP path: {name[:160]}")
                if mode and (mode & 0o170000) == 0o120000: raise HTTPException(400, "ZIP symlinks are not allowed")
                total += entry.file_size
                if total > 1024 * 1024 * 1024: raise HTTPException(413, "Uncompressed package exceeds 1 GiB")
                names.append(name)
            if "manifest.json" not in names: raise HTTPException(400, "ZIP must contain manifest.json at its root")
            if not any(n.startswith("payload/") and n.lower().endswith(".addin") for n in names): raise HTTPException(400, "ZIP payload must contain a .addin manifest")
            if not any(n.startswith("payload/") and n.lower().endswith(".dll") for n in names): raise HTTPException(400, "ZIP payload must contain at least one DLL")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8-sig"))
    except zipfile.BadZipFile: raise HTTPException(400, "Invalid ZIP package")
    except UnicodeDecodeError: raise HTTPException(400, "ZIP manifest.json must be UTF-8 JSON")
    except json.JSONDecodeError: raise HTTPException(400, "ZIP manifest.json is invalid JSON")
    if manifest_json:
        try: submitted = json.loads(manifest_json)
        except Exception: raise HTTPException(400, "manifest_json must be valid JSON")
        for key in ("id", "version", "packageType", "supportedRevitVersions"):
            if submitted.get(key) != manifest.get(key): raise HTTPException(400, f"Submitted manifest and ZIP manifest disagree on {key}")
    addon_id = addon_id or manifest.get("id"); version = version or manifest.get("version")
    if manifest.get("id") != addon_id or manifest.get("version") != version: raise HTTPException(400, "Manifest id/version must match form fields")
    if manifest.get("packageType") != "RevitAddin": raise HTTPException(400, "Only packageType=RevitAddin is supported")
    years = manifest.get("supportedRevitVersions")
    if not isinstance(years, list) or not years or not all(re.fullmatch(r"20\d{2}", str(y)) for y in years): raise HTTPException(400, "supportedRevitVersions must be a list like [\"2024\",\"2025\"]")
    manifest["supportedRevitVersions"] = [str(y) for y in years]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", str(addon_id)): raise HTTPException(400, "Invalid add-in ID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,49}", str(version)): raise HTTPException(400, "Invalid version")
    sha = hashlib.sha256(content).hexdigest(); package_id = str(uuid.uuid4()); safe_name = f"{package_id}.zip"; path = PKG_DIR / safe_name
    path.write_bytes(content); now = int(time.time())
    try:
        with db() as c:
            c.execute("INSERT INTO packages(id,addon_id,version,package_file,sha256,manifest_json,created_at,release_notes,uploaded_by) VALUES(?,?,?,?,?,?,?,?,?)",
                      (package_id, addon_id, version, safe_name, sha, json.dumps(manifest), now, release_notes[:4000], admin))
            c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)", (now, "package_published", json.dumps({"addon_id": addon_id, "version": version, "sha256": sha, "by": admin})))
    except sqlite3.IntegrityError:
        path.unlink(missing_ok=True); raise HTTPException(409, "This add-in version already exists")
    return {"package_id": package_id, "addon_id": addon_id, "version": version, "sha256": sha, "supportedRevitVersions": manifest["supportedRevitVersions"]}

@app.post("/api/admin/packages/{package_id}/revoke")
def revoke_package(package_id: str, restore: bool = False, admin: str = Depends(require_admin)):
    with db() as c:
        if not c.execute("SELECT 1 FROM packages WHERE id=?", (package_id,)).fetchone(): raise HTTPException(404, "Package not found")
        c.execute("UPDATE packages SET revoked=? WHERE id=?", (0 if restore else 1, package_id))
        c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)", (int(time.time()), "package_restored" if restore else "package_revoked", json.dumps({"package_id": package_id, "by": admin})))
    return {"status": "restored" if restore else "revoked"}

@app.post("/api/admin/users/{username}/enabled")
def set_user_enabled(username: str, enabled: bool, admin: str = Depends(require_admin)):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone(): raise HTTPException(404, "User not found")
        c.execute("UPDATE users SET enabled=? WHERE username=?", (1 if enabled else 0, username))
        if not enabled: c.execute("DELETE FROM sessions WHERE username=?", (username,))
    return {"status": "updated"}

class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=256)

@app.post("/api/admin/users/{username}/password")
def reset_password(username: str, req: PasswordReset, admin: str = Depends(require_admin)):
    salt, digest = password_hash(req.password)
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone(): raise HTTPException(404, "User not found")
        c.execute("UPDATE users SET password_salt=?,password_hash=? WHERE username=?", (salt, digest, username))
        c.execute("DELETE FROM sessions WHERE username=?", (username,))
    return {"status": "password_reset"}

@app.delete("/api/admin/device-owners")
def unassign_device(username: str, device_id: str, admin: str = Depends(require_admin)):
    with db() as c: c.execute("DELETE FROM user_devices WHERE username=? AND device_id=?", (username, device_id))
    return {"status": "unassigned"}

@app.get("/api/admin/events")
def list_events(limit: int = 200, admin: str = Depends(require_admin)):
    with db() as c: rows = c.execute("SELECT ts,device_id,event_type,details FROM events ORDER BY id DESC LIMIT ?", (min(limit, 1000),)).fetchall()
    return {"events": [dict(r) for r in rows]}

@app.post("/api/assignments", dependencies=[Depends(require_admin)])
def create_assignment(req: AssignmentRequest):
    now = int(time.time())
    with db() as c:
        if not c.execute("SELECT id FROM packages WHERE addon_id=? AND version=? AND revoked=0", (req.addon_id, req.version)).fetchone():
            raise HTTPException(404, "Package version not found")
        aid = str(uuid.uuid4())
        c.execute("INSERT INTO assignments(id,addon_id,version,target_group,created_at,enabled) VALUES(?,?,?,?,?,1)",
                   (aid, req.addon_id, req.version, req.target_group, now))
        c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)", (now, "assignment_created", json.dumps(req.model_dump())))
    return {"assignment_id": aid, "status": "created"}

@app.get("/api/portal/catalog")
def catalog(user: dict = Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT id,addon_id,version,sha256,manifest_json,created_at,release_notes FROM packages WHERE revoked=0 ORDER BY addon_id,created_at DESC,rowid DESC").fetchall()
        if user["role"] == "admin": devices = c.execute("SELECT id,hostname,revit_versions,status,last_seen FROM devices ORDER BY hostname").fetchall()
        else: devices = c.execute('''SELECT d.id,d.hostname,d.revit_versions,d.status,d.last_seen FROM devices d
          JOIN user_devices ud ON ud.device_id=d.id WHERE ud.username=? ORDER BY ud.is_primary DESC,d.hostname''', (user["username"],)).fetchall()
        owned = {r["id"] for r in devices}
        jobs = c.execute('''SELECT j.id,j.device_id,j.status,j.message,j.updated_at,p.addon_id,p.version,d.hostname
          FROM jobs j JOIN packages p ON p.id=j.package_id JOIN devices d ON d.id=j.device_id
          ORDER BY j.updated_at DESC LIMIT 300''').fetchall()
        if user["role"] != "admin": jobs = [j for j in jobs if j["device_id"] in owned]
    seen = set(); pk = []
    for r in rows:
        pk.append({"package_id": r["id"], "addon_id": r["addon_id"], "version": r["version"], "sha256": r["sha256"], "manifest": json.loads(r["manifest_json"]),
                   "created_at": r["created_at"], "release_notes": r["release_notes"], "is_latest": r["addon_id"] not in seen})
        seen.add(r["addon_id"])
    return {"packages": pk,
            "devices": [{"device_id": r["id"], "hostname": r["hostname"], "revit_versions": json.loads(r["revit_versions"] or "[]"), "status": r["status"], "last_seen": r["last_seen"]} for r in devices],
            "jobs": [dict(j) for j in jobs], "user": {"username": user["username"], "role": user["role"]}}

def _pkg_for_download(package_id: str, user: dict):
    with db() as c:
        row = c.execute("SELECT package_file,addon_id,version,revoked FROM packages WHERE id=?", (package_id,)).fetchone()
        if not row or (row["revoked"] and user["role"] != "admin"): raise HTTPException(404, "Approved package not found")
        c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)", (int(time.time()), "package_downloaded", json.dumps({"addon_id": row["addon_id"], "version": row["version"], "by": user["username"]})))
    path = PKG_DIR / row["package_file"]
    if not path.is_file(): raise HTTPException(404, "Package file missing")
    return row, path

@app.get("/api/portal/packages/{package_id}/download")
def portal_download_package(package_id: str, user: dict = Depends(current_user)):
    """Authenticated ZIP download. A browser cannot write into Revit folders; the local installer does that."""
    row, path = _pkg_for_download(package_id, user)
    return FileResponse(path, filename=re.sub(r"[^A-Za-z0-9._-]", "_", f"{row['addon_id']}-{row['version']}.zip"), media_type="application/zip")

@app.get("/api/portal/packages/{package_id}/bundle")
def portal_download_bundle(package_id: str, user: dict = Depends(current_user)):
    """One-click bundle: package.zip + installer + Install.bat. Extract, double-click Install.bat."""
    row, path = _pkg_for_download(package_id, user)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, "package.zip")
        z.write(STATIC_DIR / "Install-BIMAddinPackage.ps1", "Install-BIMAddinPackage.ps1")
        z.write(STATIC_DIR / "Install.bat", "Install.bat")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{row['addon_id']}-{row['version']}-installer.zip")
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

PUBLIC_URL = os.getenv("BIMDEPLOY_PUBLIC_URL", "").rstrip("/")

@app.post("/api/portal/packages/{package_id}/install-token")
def mint_install_token(package_id: str, user: dict = Depends(current_user)):
    """Short-lived (5 min) token the local helper uses to download one package. Any signed-in role may request."""
    with db() as c:
        row = c.execute("SELECT addon_id,version,revoked FROM packages WHERE id=?", (package_id,)).fetchone()
        if not row or row["revoked"]: raise HTTPException(404, "Approved package not found")
        raw = secrets.token_urlsafe(32); exp = int(time.time()) + 300
        c.execute("INSERT INTO install_tokens(token_hash,package_id,username,expires_at) VALUES(?,?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), package_id, user["username"], exp))
        c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)", (int(time.time()), "install_requested", json.dumps({"addon_id": row["addon_id"], "version": row["version"], "by": user["username"]})))
    return {"token": raw, "package_id": package_id, "expires_in": 300}

@app.get("/api/install/packages/{package_id}/download")
def install_download(package_id: str, token: str):
    key = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        t = c.execute("SELECT package_id,username,expires_at FROM install_tokens WHERE token_hash=?", (key,)).fetchone()
        if not t or t["package_id"] != package_id or t["expires_at"] < int(time.time()): raise HTTPException(401, "Install token invalid or expired. Click Install now again.")
        row = c.execute("SELECT package_file,sha256,addon_id,version,revoked FROM packages WHERE id=?", (package_id,)).fetchone()
        if not row or row["revoked"]: raise HTTPException(404, "Package not available")
        c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)", (int(time.time()), "package_downloaded", json.dumps({"addon_id": row["addon_id"], "version": row["version"], "by": t["username"], "via": "helper"})))
    path = PKG_DIR / row["package_file"]
    if not path.is_file(): raise HTTPException(404, "Package file missing")
    return FileResponse(path, media_type="application/zip", filename=f"{package_id}.zip", headers={"X-Package-SHA256": row["sha256"]})

@app.get("/api/portal/setup-bundle")
def setup_bundle(request: Request, user: dict = Depends(current_user)):
    """One-time per-PC setup: registers the bimstore:// handler (HKCU, no admin) so 'Install now' works."""
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in ("Setup.bat", "Register-BIMStoreProtocol.ps1", "BIMStore-Handler.ps1", "Install-BIMAddinPackage.ps1"): z.write(STATIC_DIR / n, n)
        z.writestr("config.json", json.dumps({"serverUrl": base}, indent=2))
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="BIMStore-Setup.zip"'})

@app.post("/api/portal/deploy")
def deploy(req: DeployRequest, user: dict = Depends(current_user)):
    now = int(time.time())
    if req.target not in ("current", "remote"): raise HTTPException(400, "target must be current or remote")
    if req.target == "remote" and user["role"] != "admin": raise HTTPException(403, "Remote deployment requires administrator authorization")
    if not req.device_id: raise HTTPException(400, "Choose a target device")
    with db() as c:
        device = c.execute("SELECT id,hostname FROM devices WHERE id=?", (req.device_id,)).fetchone()
        if not device: raise HTTPException(404, "Target device not found")
        if user["role"] != "admin":
            owned = c.execute("SELECT 1 FROM user_devices WHERE username=? AND device_id=?", (user["username"], req.device_id)).fetchone()
            if not owned: raise HTTPException(403, "You can install only on a device assigned to your account")
        package = c.execute("SELECT id FROM packages WHERE addon_id=? AND version=? AND revoked=0", (req.addon_id, req.version)).fetchone()
        if not package: raise HTTPException(404, "Approved package version not found")
        jid = queue_job(c, req.device_id, package["id"], now)
        c.execute("UPDATE jobs SET status='queued',updated_at=?,message=? WHERE id=?", (now, f"Requested by {user['username']}", jid))
        c.execute("INSERT INTO events(ts,device_id,event_type,details) VALUES(?,?,?,?)", (now, req.device_id, "deployment_requested", json.dumps({"job_id":jid,"addon_id":req.addon_id,"version":req.version,"requested_by":user["username"],"target":req.target})))
    return {"job_id": jid, "status": "queued", "target_hostname": device["hostname"]}

@app.get("/api/devices/jobs")
def get_jobs(x_device_id: str = Header(...), _: None = Depends(require_device)):
    with db() as c:
        rows = c.execute('''SELECT j.id AS job_id,j.status,p.id AS package_id,p.addon_id,p.version,p.sha256,p.manifest_json
          FROM jobs j JOIN packages p ON p.id=j.package_id WHERE j.device_id=? AND j.status IN ('queued','retry','deferred') ORDER BY j.created_at LIMIT 10''', (x_device_id,)).fetchall()
        out=[]
        for r in rows:
            out.append({"job_id":r["job_id"],"package_id":r["package_id"],"addon_id":r["addon_id"],"version":r["version"],"download_url":f"/api/packages/{r['package_id']}/download","sha256":r["sha256"],"manifest":json.loads(r["manifest_json"])})
            c.execute("UPDATE jobs SET status='downloading',updated_at=? WHERE id=?", (int(time.time()),r["job_id"]))
    return {"jobs":out}

@app.get("/api/packages/{package_id}/download")
def download_package(package_id: str, x_device_id: str = Header(...), _: None = Depends(require_device)):
    with db() as c:
        row=c.execute("SELECT p.package_file FROM packages p JOIN jobs j ON j.package_id=p.id WHERE p.id=? AND j.device_id=? AND j.status IN ('downloading','queued','retry','deferred')",(package_id,x_device_id)).fetchone()
    if not row: raise HTTPException(404,"Package is not assigned to this device")
    path=PKG_DIR / row["package_file"]
    if not path.is_file(): raise HTTPException(404,"Package file missing")
    return FileResponse(path, filename=f"{package_id}.zip", media_type="application/zip")

@app.post("/api/jobs/{job_id}/report")
def report_job(job_id: str, req: JobReport, x_device_id: str = Header(...), _: None = Depends(require_device)):
    allowed={"succeeded","failed","deferred","retry"}
    if req.status not in allowed: raise HTTPException(400,f"status must be one of {sorted(allowed)}")
    now=int(time.time())
    with db() as c:
        row=c.execute("SELECT id FROM jobs WHERE id=? AND device_id=?",(job_id,x_device_id)).fetchone()
        if not row: raise HTTPException(404,"Job not found for device")
        c.execute("UPDATE jobs SET status=?,updated_at=?,message=? WHERE id=?",(req.status,now,req.message[:4000],job_id))
        c.execute("INSERT INTO events(ts,device_id,event_type,details) VALUES(?,?,?,?)",(now,x_device_id,"job_"+req.status,json.dumps({"job_id":job_id,"message":req.message[:1000]})))
    return {"status":"recorded"}

@app.post("/api/admin/bootstrap", dependencies=[Depends(require_admin)])
def bootstrap_admin(req: UserCreate):
    """Create the first web admin account; disabled after any user exists."""
    if req.role != "admin": raise HTTPException(400, "Bootstrap account must have admin role")
    with db() as c:
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone(): raise HTTPException(409, "Bootstrap is disabled because a user already exists")
        salt, digest = password_hash(req.password)
        c.execute("INSERT INTO users(username,password_salt,password_hash,role,enabled,created_at) VALUES(?,?,?,'admin',1,?)", (req.username,salt,digest,int(time.time())))
        c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)",(int(time.time()),"bootstrap_admin_created",json.dumps({"username":req.username})))
    return {"username":req.username,"role":"admin","status":"created"}

@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
def create_user(req: UserCreate):
    if req.role not in ("admin", "modeller"): raise HTTPException(400, "role must be admin or modeller")
    salt, digest = password_hash(req.password); now = int(time.time())
    try:
        with db() as c:
            c.execute("INSERT INTO users(username,password_salt,password_hash,role,enabled,created_at) VALUES(?,?,?,?,1,?)", (req.username,salt,digest,req.role,now))
            c.execute("INSERT INTO events(ts,event_type,details) VALUES(?,?,?)",(now,"user_created",json.dumps({"username":req.username,"role":req.role})))
    except sqlite3.IntegrityError: raise HTTPException(409, "Username already exists")
    return {"username":req.username,"role":req.role,"status":"created"}

@app.post("/api/admin/device-owners", dependencies=[Depends(require_admin)])
def assign_device(req: OwnershipRequest):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE username=? AND enabled=1",(req.username,)).fetchone(): raise HTTPException(404,"User not found")
        if not c.execute("SELECT 1 FROM devices WHERE id=?",(req.device_id,)).fetchone(): raise HTTPException(404,"Device not found")
        if req.is_primary: c.execute("UPDATE user_devices SET is_primary=0 WHERE username=?",(req.username,))
        c.execute("INSERT INTO user_devices(username,device_id,is_primary) VALUES(?,?,?) ON CONFLICT(username,device_id) DO UPDATE SET is_primary=excluded.is_primary",(req.username,req.device_id,1 if req.is_primary else 0))
        c.execute("INSERT INTO events(ts,device_id,event_type,details) VALUES(?,?,?,?)",(int(time.time()),req.device_id,"device_assigned",json.dumps({"username":req.username,"is_primary":req.is_primary})))
    return {"status":"assigned"}

@app.get("/api/admin/overview", dependencies=[Depends(require_admin)])
def overview():
    with db() as c:
        devices=[dict(r) for r in c.execute("SELECT id,hostname,machine_id,agent_version,revit_versions,groups_json,last_seen,status FROM devices ORDER BY hostname").fetchall()]
        packages=[dict(r) for r in c.execute("SELECT id,addon_id,version,sha256,created_at,manifest_json,revoked,release_notes,uploaded_by FROM packages ORDER BY created_at DESC,rowid DESC").fetchall()]
        jobs=[dict(r) for r in c.execute("SELECT j.id,j.device_id,d.hostname,p.addon_id,p.version,j.status,j.created_at,j.updated_at,j.message FROM jobs j JOIN devices d ON d.id=j.device_id JOIN packages p ON p.id=j.package_id ORDER BY j.created_at DESC LIMIT 300").fetchall()]
        users=[dict(r) for r in c.execute("SELECT username,role,enabled,created_at FROM users ORDER BY username").fetchall()]
        owners=[dict(r) for r in c.execute("SELECT username,device_id,is_primary FROM user_devices").fetchall()]
    return {"devices":devices,"packages":packages,"jobs":jobs,"users":users,"owners":owners}


@app.on_event("startup")
def _warn_config():
    for name, val in (("BIMDEPLOY_ADMIN_TOKEN", ADMIN_TOKEN), ("BIMDEPLOY_ENROLLMENT_TOKEN", ENROLLMENT_TOKEN), ("BIMDEPLOY_DEVICE_TOKEN", DEVICE_TOKEN)):
        if not token_ok("x" * 1, val) and (len(val) < 24 or any(m in val.lower() for m in WEAK_MARKERS)):
            print(f"[WARN] {name} is missing/weak. Run: python manage.py init")
