import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    if private:
        path.chmod(0o600)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def groups(group):
    yield group
    for child in group.get("processGroups", []):
        yield from groups(child)


def summary(snapshot):
    result = Counter()
    bundles = set()
    for group in groups(snapshot["flowContents"]):
        result["processGroups"] += 1
        for key in ("processors", "controllerServices", "connections", "inputPorts", "outputPorts"):
            result[key] += len(group.get(key, []))
        for component in group.get("processors", []) + group.get("controllerServices", []):
            b = component.get("bundle") or {}
            bundles.add((b.get("group"), b.get("artifact"), b.get("version")))
    result["parameterContexts"] = len(snapshot.get("parameterContexts", {}))
    return {"counts": dict(result), "bundles": [dict(zip(("group", "artifact", "version"), x))
            for x in sorted(bundles, key=str)]}


def prepare_snapshot(snapshot):
    """Retain native schema; omit runtime state and native sensitive values.

    No scanning of scripts, URLs, comments or non-sensitive values is performed.
    Enabled processors are made STOPPED (versioned schema calls this ENABLED).
    Disabled processors remain disabled. Services are staged disabled.
    """
    data = copy.deepcopy(snapshot)
    for group in groups(data["flowContents"]):
        group.pop("componentStates", None)
        group["scheduledState"] = "ENABLED"
        for p in group.get("processors", []):
            if p.get("scheduledState") != "DISABLED":
                p["scheduledState"] = "ENABLED"
        for p in group.get("inputPorts", []) + group.get("outputPorts", []):
            if p.get("scheduledState") != "DISABLED":
                p["scheduledState"] = "ENABLED"
        for service in group.get("controllerServices", []):
            service["scheduledState"] = "DISABLED"
    contexts = data.get("parameterContexts", {})
    for context in contexts.values() if isinstance(contexts, dict) else contexts:
        for parameter in context.get("parameters", []):
            if parameter.get("sensitive"):
                parameter.pop("value", None)
    return data


def parameter_requirements(snapshot):
    contexts = snapshot.get("parameterContexts", {})
    return {c["name"]: [{"name": p["name"], "sensitive": bool(p.get("sensitive")),
                        "hasExportedValue": p.get("value") is not None}
                       for p in c.get("parameters", [])]
            for c in (contexts.values() if isinstance(contexts, dict) else contexts)}


def export_bundle(client, output, name, group_id=None, registry=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise ValueError("Bundle name must be lowercase letters, digits and hyphens")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    about = client.get("/flow/about")["about"]
    group_id = group_id or client.root_id()
    snapshot = client.get(f"/process-groups/{group_id}/download?includeReferencedServices=true&includeComponentState=false")
    snapshot = prepare_snapshot(snapshot)
    write_json(output / "flow.json", snapshot)
    write_json(output / "parameters.required.json", parameter_requirements(snapshot))
    # Registry clients are inventory, not blindly replayable API responses.
    clients = client.get("/controller/registry-clients").get("registries", [])
    inventory = []
    for entity in clients:
        component = entity.get("component", {})
        inventory.append({k: component.get(k) for k in ("name", "type", "bundle")})
    write_json(output / "registry-clients.inventory.json", inventory)
    manifest = {"schemaVersion": 1, "name": name, "nifiVersion": about["version"],
                "nifiImage": "apache/nifi:" + about["version"], "nginxImage": "nginx:1.27-alpine",
                "registryImage": "apache/nifi-registry:" + about["version"],
                "registryEnabled": registry, "flowFile": "flow.json",
                "flowSha256": digest(output / "flow.json"), "artifacts": [], **summary(snapshot)}
    # Retain explicitly packaged binaries on subsequent CI exports.
    if (output / "release.json").exists():
        manifest["artifacts"] = read_json(output / "release.json").get("artifacts", [])
    write_json(output / "release.json", manifest)
    return manifest


def validate_bundle(path):
    path = Path(path).resolve()
    if (path / "config.json").exists():
        from .configuration import validate_config
        config = validate_config(path / "config.json")
        return {"schemaVersion": 2, "name": config["name"], "counts": config["flow"]["counts"]}
    release = read_json(path / "release.json")
    if release.get("schemaVersion") != 1:
        raise ValueError("Unsupported bundle schema")
    flow = (path / release["flowFile"]).resolve()
    if not flow.is_relative_to(path) or digest(flow) != release["flowSha256"]:
        raise ValueError("Flow path/checksum mismatch")
    snapshot = read_json(flow)
    if "flowContents" not in snapshot or "rootGroup" in snapshot:
        raise ValueError("Expected native exported flow definition, not persisted flow.json.gz")
    for group in groups(snapshot["flowContents"]):
        components = [group]
        for key in ("processors", "controllerServices", "inputPorts", "outputPorts"):
            components += group.get(key, [])
        if any(c.get("scheduledState", "ENABLED") not in ("ENABLED", "DISABLED") for c in components):
            raise ValueError("Native snapshot scheduledState must be ENABLED/DISABLED, not REST run-state STOPPED")
    for artifact in release.get("artifacts", []):
        p = (path / artifact["file"]).resolve()
        if not p.is_relative_to(path) or artifact["target"] not in ("lib", "extensions", "scripts"):
            raise ValueError("Invalid artifact path/target")
        if digest(p) != artifact["sha256"]:
            raise ValueError("Artifact checksum mismatch: " + artifact["file"])
    return release
