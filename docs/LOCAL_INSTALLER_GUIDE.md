# Local download-and-install guide (without a resident agent)

## What this mode does

1. The modeller signs in to the BIM Add-in Store.
2. The modeller clicks **Download ZIP for local install**.
3. The modeller downloads `Install-BIMAddinPackage.ps1` once (or receives it from IT).
4. With all Revit sessions closed, the modeller runs the installer and selects the ZIP.
5. The installer validates ZIP paths and the manifest, copies the add-in payload into the correct Revit version directory, rewrites `.addin` Assembly paths to the actual installed DLL, backs up the previous add-in folder, and prints the result.

This is a one-shot installer, not a continuously running Windows agent. The browser itself cannot write into Revit folders. This mode installs only on the computer where the script is run and does not send a trustworthy installation result back to the web portal.

## Revit folders

- Per-user (`installScope: "user"`): `%APPDATA%\Autodesk\Revit\Addins\2025` (and each version listed in `supportedRevitVersions`). This normally avoids administrator rights.
- Machine-wide (`installScope: "machine"`): `%ProgramData%\Autodesk\Revit\Addins\2025` (and each supported version). Requires an elevated PowerShell session.
- `.addin` manifest files must be directly inside each version directory. The payload DLLs can live in a subfolder as long as the `.addin` Assembly element points to the actual DLL path.

## Run it

Open PowerShell in the folder containing the downloaded script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\Install-BIMAddinPackage.ps1
```

Choose the package ZIP when the file picker opens. The execution-policy setting only applies to that PowerShell process. Follow your organization's signed-script policy in production; do not weaken machine-wide policy.

## Package requirements

The ZIP root must contain `manifest.json` and `payload/`. The payload must include at least one `.addin` file and at least one `.dll`. The manifest must include `id`, `version`, `supportedRevitVersions`, and optionally `installScope`. For standard modellers without local administrator rights, publish an approved package with `"installScope": "user"`.

## Limitations and production gates

- Test against the exact Revit versions and actual signed DLLs before deployment. The sample package is not a functional add-in.
- The pilot script is not Authenticode-signed and has not been executed on a Windows/Revit host in this environment. Have IT review, sign, and test it before production.
- Local download/install cannot perform remote installs or central status reporting. For those, use the existing managed agent or an enterprise endpoint management system.
- Add enterprise SSO, HTTPS, package signature verification, audit retention, backups, and security testing before production.
