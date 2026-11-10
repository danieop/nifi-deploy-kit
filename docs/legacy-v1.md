# Legacy schema 1 / v0.1

For an existing schema 1 bundle containing `release.json`, the old interface
continues to work:

```bash
./deploy.sh --bundle OLD_BUNDLE --env local/deploy.env \
  --parameters local/parameters.local.json --components local/components.local.json
```

The old example files live under `examples/legacy/`. Schema 1 uses the fixed
`infrastructure/compose.yml` baseline. Do not mix those flags with `--config`.
New exports always produce schema 2: flow.json, config.json and assets/. Copy the
complete new bundle, edit config.json and use `./deploy.sh --config FILE`.

Do not point a new deployment project at a different project's volumes or erase
old volumes. This release does not automatically adopt/convert schema 1 runtime
state or change NiFi versions. The v0.1 test report is historical and describes
that earlier setup; use the schema 2 report for current local URLs/results.
