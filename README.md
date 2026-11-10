# NiFi Deploy Kit

Export a running **Docker Compose + NiFi** deployment, then deploy it on another
single Docker host by editing **one `config.json`**. Native NiFi flow JSON stays
separate. Python 3.10+ standard library, Docker Compose and OpenSSH; no pip packages.
Tested baseline: NiFi 2.12.0, HTTP NiFi behind Nginx Basic Auth.

```text
Source host (read-only)
  NiFi GET API + SSH Docker/Compose inspection + bind-mounted files
                 ↓ GitHub Actions
bundles/node235/
  flow.json       native flow, referenced services and reachable parameter contexts
  config.json     captured Docker model + deployment + editable NiFi properties
  assets/         required mounted files, NARs/JARs, startup scripts, Nginx config
                 ↓ download/copy, edit config.json
Destination host: ./deploy.sh --config local/node235/config.json
```

The Docker model comes from the **source**, not a fixed Compose template. The
exporter reads resolved Compose and running-container launch settings. Image
digests, entrypoint/command, declared/custom environment, ports, restart policy,
resource limits and mounts are retained. Source host bind paths are relocated;
source volumes become fresh project-owned named volumes. Source volume data,
queues, checkpoints and Registry history are not copied.

## Deploy on a customer host

```bash
git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
# Checkout the reviewed release/tag supplied by the maintainer.
mkdir -p local
chmod 700 local
cp -a bundles/node235 local/node235
chmod 600 local/node235/config.json
${EDITOR:-vi} local/node235/config.json
./deploy.sh --config local/node235/config.json
```

Edit these sections in **that copied file**:

| Section | What the admin changes |
|---|---|
| `deployment.projectName` | Unique Compose project for this instance |
| `deployment.directory` | Writable runtime directory; relative to `config.json`, or absolute |
| `deployment.apiUrl` | Local gateway URL; port must match the gateway's published port |
| `deployment.gatewayAuth.username/password` | New destination UI/API Basic Auth credentials |
| `docker.compose.services` | Source Docker settings: host ports/bind IPs, heap, resources, mounts, environment |
| `parameters` | NiFi Parameter Context values: destination hosts, URLs, accounts, secrets |
| `components[].properties` | Processor/service properties, including endpoints not parameterized in NiFi |
| `localFiles` | Paths to customer-local certificate/key files if the source required them |

**Review source heap and published ports before deploy.** The supplied sources
use a 16 GiB maximum heap. The export preserves it; select an appropriate value
for the customer's RAM. Source ports/bind addresses are also preserved, including
the optional Registry's published port. To keep UI local, set gateway port
`host_ip` to `127.0.0.1`.

`__REQUIRED_SECRET__` is an editable placeholder, never a recovered password.
Native sensitive parameter/property values and sensitive Docker env/label keys
are masked. Optional unset sensitive properties are `null`. Parameter references
such as `#{Password}` remain references. Source `.htpasswd` is not copied: the
script generates a new one from `deployment.gatewayAuth`.

Gateway/Docker/local-file placeholders must be replaced before creating
containers. NiFi placeholders can remain for a **staged** import; they are not
sent to NiFi. The report lists their paths. All remaining placeholders block
`--start`. Secrets inside ordinary script/URL/text fields cannot be reliably
classified by NiFi metadata; review them before publishing. The exporter does
not claim to scrub arbitrary application code or binary content.

The default import is staged stopped. Check the UI and the runtime `report.json`,
correct endpoints/credentials and validation, then:

```bash
./deploy.sh --config local/node235/config.json --start
```

Controller-service references and component wiring remain native. The script
maps original component IDs to imported IDs when applying later changes. Names
are descriptive; keep `sourceId` and other exported identifiers unchanged.

## Export manually

Source prerequisites: API GET access, SSH, Python 3, Docker Compose, trusted SSH
host keys, Docker read permission (or `--docker-sudo`). No source file is written,
no container is exec'd/restarted and no NiFi configuration is changed. Normal
SSH/sudo/API audit logs may still be generated.

```bash
# Private API credentials: {"username":"...","password":"..."}
./export.sh --url http://SOURCE:9080/nifi --name customer-app \
  --auth-file local/source-auth.json \
  --docker-host ubuntu@SOURCE \
  --compose-file /home/ubuntu/nifi/compose.yml \
  --compose-project nifi --ssh-known-hosts sources/known_hosts \
  --output bundles/customer-app
```

Use SSH agent/default keys or `--ssh-identity`. Password authentication can use
`NIFI_SOURCE_SSH_PASSWORD` from a private environment. `--docker-sudo` uses
`sudo -n` when passwordless, otherwise `NIFI_SOURCE_SUDO_PASSWORD` (falls back to
the SSH password), sent over stdin. Never put passwords in command arguments.
For same-host export, replace `--docker-host` with `--docker-local`.

## GitHub Actions

- **Validate:** unit tests, checksums, native flow counts, shell and Compose checks.
- **Export NiFi:** manual, trusted default-branch code only, trusted self-hosted
  runner labelled `self-hosted, linux, x64, nifi-network` with source network access.
  Reads `sources/hosts.json` and verified `sources/known_hosts`.
- Secrets: `NIFI_SOURCE_USERNAME`, `NIFI_SOURCE_PASSWORD`; optional
  `NIFI_SOURCE_SSH_KEY`, or `NIFI_SOURCE_235_SSH_PASSWORD` and
  `NIFI_SOURCE_237_SSH_PASSWORD`. These source users use Docker through sudo.
- Optional URL overrides: repository variables `SOURCE_235_URL`, `SOURCE_237_URL`.
- Exports both bundles, validates, uploads `nifi-export-RUN_ID`, optionally pushes
  an export branch. A maintainer opens/merges its PR; no direct pushes to main and
  no changes to repository approval settings.

```bash
gh workflow run export.yml -f source=both -f publish=true
gh run download RUN_ID -n nifi-export-RUN_ID -D local/downloaded
# Edit local/downloaded/node235/config.json before deploying.
./deploy.sh --config local/downloaded/node235/config.json
```

Source credentials and edited customer config must stay outside Git. `local/` is
ignored. A network runner must be online for each export; the test runner was
ephemeral, not a permanently installed service.

## Redeploy and scope

Same config: reuse the recorded group and volumes. Changed NiFi property/parameter
values are applied to stopped/disabled components; unchanged values are skipped.
A different flow or Docker model requires `--overwrite-config` and a stopped,
drained target. The old native flow is backed up locally before replacement.
Do not delete volumes or the runtime deployment state to retry a failure.

This is a new deployment, not a disk clone. Registry Clients are inventory only;
client tokens, external systems, Reporting Tasks, Parameter Providers, Flow
Analysis Rules and arbitrary host-level NiFi settings are not replayed. The
baseline requires Compose services running once each, a NiFi service and an
HTTP Basic Auth Nginx gateway with a `.htpasswd` bind. Docker Swarm/Kubernetes,
TLS/LDAP/OIDC, external Docker networks, Compose secrets/config objects and image
upgrades are outside this tested path. Unsupported models fail explicitly.

Private key/certificate files are represented by `localFiles` placeholders,
not published. Bind-mounted application data is initialized empty. Bind assets
are capped at 64 MiB/file and 128 MiB/package; symlinks require explicit packaging.
Registry storage without source mounts remains ephemeral, matching that source;
add destination storage if persistence is required.

[Hướng dẫn admin tiếng Việt](docs/admin-vi.md) ·
[Config reference](docs/config-reference.md) ·
[Legacy v0.1 interface](docs/legacy-v1.md)

```bash
python3 -m unittest discover -s tests -v
python3 -m nifi_deploy validate bundles/node235 bundles/node237
```

Tooling is MIT licensed; exported application content and third-party assets
retain their respective ownership/licenses.
