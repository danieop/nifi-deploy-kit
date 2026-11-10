# NiFi Deploy Kit

Export native Apache NiFi flow definitions with GitHub Actions and deploy a
standalone NiFi instance on one Docker Compose host. Python 3.10+ standard library;
no pip dependencies. Tested baseline: NiFi **2.12.0**, Nginx **1.27-alpine**.

## Workflow

1. `export.sh` reads a running source through GET requests only. It exports the
   selected group (root by default), referenced services, reachable parameter
   contexts, bundle dependencies and Registry Client inventory.
2. GitHub Actions exports both configured sources, validates bundles, uploads a
   downloadable artifact and optionally pushes a feature branch when data changes.
3. A maintainer opens/merges the PR. Nothing pushes directly to `main`.
4. The customer clones a reviewed commit/tag, creates local configuration and
   runs `deploy.sh`. Compose starts NiFi/Nginx (and optional Registry); the NiFi
   API imports the application and applies local parameter overrides.

Imports are **staged stopped by default**. Original DISABLED processors stay
disabled. Export never stops or reconfigures the source. No extra scanning of
scripts, URLs, comments or non-sensitive values is performed. Only native
sensitive parameter values are omitted. Public flow definitions may contain
non-sensitive source environment values, as requested for this project.

## Customer quick start

Prerequisites: Linux, Python 3.10+, Docker Engine or Docker Desktop and Docker
Compose, Docker permission for the current user, available ports/storage/RAM,
access to image registries and application dependencies.

```bash
git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
# Select a reviewed release/tag/commit before deploying.
mkdir -m 700 local
cp examples/deploy.env.example local/deploy.env
chmod 600 local/deploy.env
# Edit project, absolute deployment directory, port, account and memory.
${EDITOR:-vi} local/deploy.env

./deploy.sh --bundle bundles/node235 --env local/deploy.env
```

Select `bundles/node237` for the no-Registry profile. `node235` also starts a
Registry container with persistent named storage. Each bundle imports one
managed application group containing its exported hierarchy. A destination root
group remains outside that application group.

The command prints the local UI URL and writes `report.json`, `owner.json` and
`deployment.json` into `DEPLOY_DIRECTORY`. All actual NiFi configuration, key,
state and repositories persist in project-specific **named Docker volumes**.
Only Nginx is published, on loopback by default. Set `BIND_ADDRESS=0.0.0.0` when
the customer intentionally needs access through the host network interface.

## Parameters and activation

Consult `bundles/<name>/parameters.required.json` for exported context/parameter
names and sensitivity. Supply only the values that must change in a local JSON:

```bash
cp examples/parameters.local.example.json local/parameters.local.json
chmod 600 local/parameters.local.json
${EDITOR:-vi} local/parameters.local.json
./deploy.sh --bundle bundles/node235 --env local/deploy.env \
  --parameters local/parameters.local.json
```

For a direct processor/service property that is not parameterized, use optional
`--components local/components.local.json`; see
`examples/components.local.example.json`. `groups` is the relative path of group
names below the managed application; `[]` selects its root. `kind` is `processor`
or `controller-service`, and `name` must identify exactly one component in that
group. Use the exact NiFi API property names. The target component must be stopped
or disabled. Omitted properties are retained. This also supports direct sensitive
properties without putting their values into the public bundle.

Omitted parameters retain their existing/exported values. Null overrides are
rejected. A newly imported sensitive parameter without a value needs a value
from the customer's secret source; GET/export cannot recover the old secret.
An infrastructure/import success is **not** proof of business readiness: inspect
`configurationComplete` and `invalidProcessors` in the report.

```bash
# Only after checking endpoints, secrets, dependencies and application behavior:
./deploy.sh --bundle bundles/node235 --env local/deploy.env \
  --parameters local/parameters.local.json --start
```

No production queue is purged and no source processor is started by these tests.

## Redeployment

Run the same command to reuse volumes and the recorded group ID. The same flow
checksum is not imported twice. Changing the flow requires `--overwrite-config`;
the target group must have no queued FlowFiles or active tasks. The current flow
definition is saved locally before replacement. Keep NiFi on the same version;
upgrades and migration of source queue/checkpoints are separate operations.

An existing unrelated Compose project or an unowned nonempty deployment
directory is rejected. This kit does not automatically adopt the anonymous
volumes on the two original VMs. Do not use `docker compose down -v` on a customer
deployment unless intentionally destroying its data.

## GitHub Actions

`Validate` runs on GitHub-hosted runners for pushes/PRs. `Export NiFi` is manual
(`workflow_dispatch`), uses only default-branch code and needs a trusted runner
with labels `self-hosted, linux, x64, nifi-network` that can reach the private
source network. Fork/PR jobs never run on this network runner.

Repository Secrets:

- `NIFI_SOURCE_USERNAME`: source Nginx Basic Auth user.
- `NIFI_SOURCE_PASSWORD`: source Nginx Basic Auth password.

Repository Variables:

- `SOURCE_235_URL`: source URL ending in `/nifi` or `/nifi-api`.
- `SOURCE_237_URL`: the other source URL.

```bash
gh workflow run export.yml -f source=both -f publish=true
gh run list --workflow export.yml
gh run download RUN_ID -n nifi-export-RUN_ID -D local/downloaded
```

The workflow never changes repository approval settings. If it pushes an export
branch, open its printed compare URL or use `gh pr create --base main --head
BRANCH`. A trusted maintainer merges after validation. Downloaded bundles are
checksum-validated by the same deploy command:

```bash
./deploy.sh --bundle local/downloaded/node235 --env local/deploy.env
```

## Manual export

```bash
cp examples/source-auth.example.json local/source-auth.json
chmod 600 local/source-auth.json
${EDITOR:-vi} local/source-auth.json
./export.sh --url http://SOURCE:9080/nifi --name customer-app \
  --output bundles/customer-app --auth-file local/source-auth.json
python3 -m nifi_deploy validate bundles/customer-app
```

For CI, credentials are read from environment variables instead. The source URLs
and credentials need not be placed in committed manifests. Export is not a
transactional backup while users are editing the source; export during a period
without configuration edits and validate the result.

## Scope and optional configuration

Required: exported native snapshot, fixed image versions, required binaries,
project/directory, available port, Nginx credentials, and parameters required by
processors the admin intends to enable. Heap/port have defaults.

Optional: Registry, external provider integrations, TLS, custom storage layout,
and automatic activation. Registry Client output is inventory, **not an automatic
recreation of GitLab clients/tokens**. Reporting Tasks, Parameter Providers and
Flow Analysis Rules are not currently replayed (none were configured in the
inspected source application). Initial host-level settings are the Compose
baseline, not an arbitrary clone of every `nifi.properties` setting.

The source applications use external RabbitMQ/Elasticsearch/Kafka/GitLab systems.
The kit does not provision those systems or migrate their data. Admins must
configure the corresponding reachable addresses/accounts on the destination.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m nifi_deploy validate bundles/node235 bundles/node237
```

See [Vietnamese admin guide](docs/admin-vi.md) and the repository `docs/` for
the actual end-to-end test report. Tooling is MIT licensed; third-party binaries
and exported application content retain their respective ownership/licenses.
