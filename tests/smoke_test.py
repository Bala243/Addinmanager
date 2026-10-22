"""Local smoke test for the BIM Add-in Store pilot API. Run: python tests/smoke_test.py"""
import io, json, os, sys, tempfile, zipfile
from pathlib import Path

server_dir = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(server_dir))
_tmp = tempfile.TemporaryDirectory(prefix="bim-store-test-")
os.environ["BIMDEPLOY_HOME"] = _tmp.name
os.environ["BIMDEPLOY_ADMIN_TOKEN"] = "admin-test-token-123456789"
os.environ["BIMDEPLOY_ENROLLMENT_TOKEN"] = "enroll-test-token-123456789"
os.environ["BIMDEPLOY_DEVICE_TOKEN"] = "device-test-token-123456789"
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)
admin_headers = {"X-Admin-Token": os.environ["BIMDEPLOY_ADMIN_TOKEN"]}
assert client.get("/").status_code == 200
assert client.post("/api/admin/bootstrap", headers=admin_headers, json={"username":"admin","password":"correct-horse-battery-99","role":"admin"}).status_code == 200
admin_login = client.post("/api/auth/login", json={"username":"admin","password":"correct-horse-battery-99"})
assert admin_login.status_code == 200
admin_auth = {"Authorization": "Bearer " + admin_login.json()["access_token"]}

r = client.post("/api/devices/enroll", json={"hostname":"PILOT-PC","machine_id":"12345678-1234-1234-1234-123456789012","enrollment_token":os.environ["BIMDEPLOY_ENROLLMENT_TOKEN"],"revit_versions":["2024"],"groups":["Pilot"]})
assert r.status_code == 200, r.text
device_id = r.json()["device_id"]
assert client.post("/api/admin/users", headers=admin_headers, json={"username":"modeller","password":"correct-horse-battery-99","role":"modeller"}).status_code == 200
assert client.post("/api/admin/device-owners", headers=admin_headers, json={"username":"modeller","device_id":device_id,"is_primary":True}).status_code == 200

manifest = {"id":"BIM.Exporter","version":"1.0.0","packageType":"RevitAddin","supportedRevitVersions":["2024"],"description":"Pilot test package"}
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("manifest.json", json.dumps(manifest))
    z.writestr("payload/BIM.Exporter.addin", "<RevitAddIns/>")
    z.writestr("payload/BIM.Exporter.dll", "test-dll-content")
r = client.post("/api/packages", headers=admin_headers, data={"addon_id":"BIM.Exporter","version":"1.0.0","manifest_json":json.dumps(manifest)}, files={"file":("BIM.Exporter.zip",buf.getvalue(),"application/zip")})
assert r.status_code == 200, r.text

modeller_login = client.post("/api/auth/login", json={"username":"modeller","password":"correct-horse-battery-99"})
modeller_auth = {"Authorization":"Bearer " + modeller_login.json()["access_token"]}
download = client.get(f"/api/portal/packages/{r.json()['package_id']}/download", headers=modeller_auth)
assert download.status_code == 200 and download.headers.get("content-type", "").startswith("application/zip")
assert client.post("/api/portal/deploy", headers=modeller_auth, json={"addon_id":"BIM.Exporter","version":"1.0.0","device_id":device_id,"target":"remote"}).status_code == 403
assert client.post("/api/portal/deploy", headers=modeller_auth, json={"addon_id":"BIM.Exporter","version":"1.0.0","device_id":device_id,"target":"current"}).status_code == 200
agent_headers = {"X-Device-Token":os.environ["BIMDEPLOY_DEVICE_TOKEN"],"X-Device-Id":device_id}
assert client.post("/api/devices/heartbeat", headers=agent_headers, json={"hostname":"PILOT-PC","agent_version":"1.0.0","revit_versions":["2024"],"groups":["Pilot"]}).status_code == 200
jobs = client.get("/api/devices/jobs", headers=agent_headers)
assert jobs.status_code == 200 and len(jobs.json()["jobs"]) == 1
job = jobs.json()["jobs"][0]
assert client.post(f"/api/jobs/{job['job_id']}/report", headers=agent_headers, json={"status":"succeeded","message":"Installed in test"}).status_code == 200
catalog = client.get("/api/portal/catalog", headers=modeller_auth)
assert catalog.status_code == 200 and catalog.json()["jobs"][0]["status"] == "succeeded"

bad = io.BytesIO()
with zipfile.ZipFile(bad, "w") as z:
    z.writestr("manifest.json", json.dumps({**manifest,"id":"BIM.Bad"}))
    z.writestr("../escape.txt", "bad")
    z.writestr("payload/a.addin", "x")
    z.writestr("payload/a.dll", "x")
r = client.post("/api/packages", headers=admin_headers, data={"addon_id":"BIM.Bad","version":"1.0.0","manifest_json":json.dumps({**manifest,"id":"BIM.Bad"})}, files={"file":("bad.zip",bad.getvalue(),"application/zip")})
assert r.status_code == 400, r.text
print("PASS: login/bootstrap, device enrollment, account/device ownership, package validation, self-service deploy, remote authorization, authenticated local ZIP download, agent polling/reporting, real status, ZIP traversal rejection")
_tmp.cleanup()
