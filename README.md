# NiFi Deploy Kit

Export a running NiFi Docker Compose deployment from an Ubuntu workstation or
GitHub Actions, then deploy it on another single Docker host by editing one
`config.json`.

```text
Source: NiFi GET API + read-only SSH Docker/file inspection
                         ↓
Bundle: flow.json + config.json + assets/
                         ↓ edit config.json
Destination: ./deploy.sh --config local/customer-a/config.json
```

- `flow.json`: native NiFi flow, referenced controller services and reachable parameter contexts.
- `config.json`: captured Docker Compose model, destination settings and editable NiFi values.
- `assets/`: checksummed bind-mounted scripts, Nginx configuration and NAR/JAR dependencies.

Python 3.10+ standard library; no pip packages. Tested NiFi baseline: **2.12.0**,
standalone Docker Compose with HTTP NiFi behind Nginx Basic Auth.

## Export

For an Ubuntu workstation already connected to NetBird, follow the
[local export guide](docs/export-ubuntu-netbird.md). It runs independently of
GitHub Actions and does not require Docker on the workstation.

Open [Actions → Export NiFi → Run workflow](https://github.com/danieop/nifi-deploy-kit/actions/workflows/export.yml)
on `main`. Enter bundle name, NiFi URL, SSH `user@host`, source Compose file and
the name of a credential Secret. Other inputs have defaults.

Create one GitHub Secret per source using
[examples/export-credentials.example.json](examples/export-credentials.example.json).
It contains API/SSH credentials and verified `knownHosts` content. The trusted
self-hosted Linux/x64 runner must reach the source and have the selected runner
label (`nifi-network` by default). Source needs Python 3, Docker Compose and
Docker/file read permission or sudo.

Each run uploads one artifact `nifi-export-RUN_ID`, retained for 14 days, with
`flow.json`, `config.json`, `assets/` at its root. Optional `publish` creates a
review branch containing only `bundles/<bundle_name>`. A maintainer opens and
merges its PR. Main, releases and customer deployments do not update automatically.

[Export workflow guide](docs/export-workflow.md)

## Deploy

Run on the destination host, directly or after SSH login. The destination does
not need SSH access to the source. It needs Docker + Compose, Python 3.10+, Docker
permission, writable storage, suitable RAM and access to container images and
application backends.

```bash
git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
mkdir -p local
chmod 700 local

# Download the selected run; replace RUN_ID with its actual ID.
gh run download RUN_ID --repo danieop/nifi-deploy-kit \
  -n nifi-export-RUN_ID -D local/customer-a
chmod 600 local/customer-a/config.json
${EDITOR:-vi} local/customer-a/config.json
./deploy.sh --config local/customer-a/config.json
```

Alternatively, copy a bundled example before editing:
`cp -a bundles/node235 local/customer-a` (or `bundles/node237`). These are reusable
example exports; workflow source servers are entered per run.

Edit destination project/directory, gateway credentials, Docker ports/bind IPs,
heap/resources, backend endpoints and secrets in `config.json`. The source heap
is preserved; size it for the destination. Replace `__REQUIRED_SECRET__` values.
Gateway/Docker/file placeholders block deployment. NiFi placeholders may remain
for staged import and are listed in the runtime report.

Processors are staged stopped by default. Inspect validation and backend access,
then run the same command with `--start`. Redeploy reuses group IDs and volumes;
flow/Docker/asset replacement requires `--overwrite-config` and a stopped,
drained target. Keep runtime state and named volumes.

[Hướng dẫn admin](docs/admin-vi.md) · [Config reference](docs/config-reference.md)

## Scope

Image references are pinned to digests when available. Host binds are relocated;
source volumes become fresh named volumes. Source `.htpasswd` is regenerated
from destination credentials; private certificates/keys require local files.
Native sensitive values and sensitive Docker environment/label keys are masked.
Arbitrary script/URL/text/binary content is not guaranteed to be scrubbed.
Artifacts in this public repo are downloadable by GitHub users with read access;
turning off publish does not make them private.

The kit does not transfer queue/content, checkpoints/state, provenance or
Registry history, provision business backends, replay Registry clients/tokens,
Reporting Tasks, Parameter Providers or Flow Analysis Rules, or perform NiFi
version upgrades. Registry storage follows the captured Docker model. TLS/LDAP/
OIDC, cluster, Kubernetes/Swarm, external networks, Compose secret/config objects,
symlink assets and arbitrary bind-mounted NiFi conf are outside the supported
baseline. Assets are capped at 64 MiB/file and 128 MiB/package.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m nifi_deploy validate bundles/node235 bundles/node237
./export.sh --help
./deploy.sh --help
```

Tooling is MIT licensed. Exported applications and third-party assets retain
their respective ownership/licenses.
