Create a release ZIP with this structure:

manifest.json
payload\BIM.ExampleAddin.dll
payload\BIM.ExampleAddin.addin
payload\AnyDependency.dll

The .addin file should contain a valid Revit add-in manifest. Its <Assembly> value can be relative to the .addin file in payload; the pilot agent converts relative paths to absolute paths after installation.

Do not upload a ZIP containing untrusted code. The server stores a SHA-256 hash and the agent verifies it before extraction. Hash verification protects transfer integrity; it does not prove that the publisher is trusted. Use signed packages and an approval process in production.
