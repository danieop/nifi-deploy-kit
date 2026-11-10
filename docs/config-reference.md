# Bundle schema 2

`flow.json` is a native versioned flow definition. `config.json` is the single
customer-editable file, not a native NiFi format. Its `docker.compose` section is
a native Compose JSON model. All paths except `deployment.directory`, `localFiles`
values and absolute customer bind paths are relative to the config's directory.

| Key | Meaning |
|---|---|
| `schemaVersion` | `2` |
| `name`, `nifiVersion` | Export identity and required baseline version; keep unchanged |
| `flow.file`, `flow.sha256` | Native snapshot path and checksum; keep unchanged |
| `flow.counts`, `flow.bundles` | Verification expectations; keep unchanged |
| `deployment.projectName` | Destination Compose project |
| `deployment.directory` | Runtime/state/Compose/assets directory, relative or absolute |
| `deployment.apiUrl` | Local HTTP Basic Auth gateway URL including published port |
| `deployment.gatewayAuth` | New username/password and generated htpasswd relative file path |
| `docker.nifiService`, `docker.gatewayService` | Roles in captured Compose services |
| `docker.compose` | Resolved source Compose plus actual container launch settings; editable |
| `assets[]` | Checksummed packaged files and source permissions; do not edit accidentally |
| `localFiles` | Relative destination file → customer-local source path; fill placeholders |
| `parameters` | Context name → parameter name → string value; omitted keys are not overridden |
| `sensitiveParameters` | Metadata naming native sensitive parameters |
| `components[]` | Original component `sourceId`, `kind`, descriptive `groups`/`name`, editable `properties` |
| `components[].sensitiveProperties` | Metadata naming sensitive properties; keep unchanged |
| `registryClientsInventory` | Inventory only; never includes registry tokens |
| `exportNotes` | Documented portability/masking changes |

NiFi property values must be strings or null. Null clears/leaves an optional
property unset. Parameter overrides must be strings. Omit a parameter override
to preserve its value on redeploy; native sensitive source values cannot be
recovered on a new destination. `#{name}` references and service wiring stay
native. Secret markers are skipped for staged import/update and block `--start`.
Never replace a secret marker with the literal NiFi mask `********`.

The generated runtime `compose.json` escapes dollars for Compose interpolation;
admin inputs remain literal. Actual config.json passwords, runtime access.json,
reports and flow backups are private local files. Runtime directories are mode
700 and private JSON files mode 600; bind-mounted generated htpasswd is readable
by the gateway but protected by the enclosing runtime directory.

Exporter behavior: staging/validation completes before replacing an existing
managed output bundle. Removed source assets do not survive as stale exported
files. Image digests are used when available. Bind paths are relocated to assets;
volume identities/data are never adopted from the source host. Source container
names and project-resolved default network names are not reused on the target.
Registry volumes, when present, get an ownership init service for fresh storage.

Source environment keys containing password/passwd/secret/token/private-key/
API-key/credential are masked (username keys and empty values excluded). Source
htpasswd and certificate/private-key files are not copied. Native sensitivity
is determined from exported descriptors plus GET presence checks on affected
components. No plaintext recovery and no arbitrary script/URL/binary scrubber.

The old infrastructure template and legacy flags remain only for schema 1
compatibility. Schema 2 deployment uses the captured `docker.compose` model.
