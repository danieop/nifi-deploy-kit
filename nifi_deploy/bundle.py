import copy
import hashlib
import json
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


def validate_bundle(path):
    from .configuration import validate_config
    config = validate_config(Path(path).resolve() / "config.json")
    return {"schemaVersion": config["schemaVersion"], "name": config["name"], "counts": config["flow"]["counts"]}
