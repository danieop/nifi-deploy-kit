import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .api import ApiError, Client, api_path
from .bundle import digest, groups, read_json, validate_bundle, write_json


ROOT = Path(__file__).resolve().parent.parent


def load_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("Invalid local env assignment")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def run(command, *, env=None, data=None, capture=True):
    p = subprocess.run(command, env=env, input=data, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None)
    if p.returncode:
        # CLI stderr may echo environment/config. Keep errors free of credentials.
        raise RuntimeError(f"Command failed ({p.returncode}): " + " ".join(command[:3]))
    return p.stdout if capture else ""


def status(client, group_id):
    return client.get(f"/flow/process-groups/{api_path(group_id)}/status")["processGroupStatus"]["aggregateSnapshot"]


def import_payload(snapshot):
    # /process-groups/import consumes ProcessGroupUploadEntity, unlike replace.
    return {"flowSnapshot": snapshot, "groupName": snapshot["flowContents"]["name"],
            "revisionDTO": {"version": 0}, "positionDTO": {"x": 0, "y": 0}}


def select_source(actual, candidates):
    by_id = [p for p in candidates if p.get("identifier") == actual.get("versionedComponentId")]
    if len(by_id) == 1:
        return by_id[0]
    by_name = [p for p in candidates if p.get("name") == actual.get("name")]
    by_position = [p for p in by_name if p.get("position", {}) == actual.get("position", {})]
    matches = by_position if by_position else by_name
    if len(matches) != 1:
        raise ValueError("Cannot uniquely map imported component to its source definition")
    return matches[0]


def enable_referenced_services(client, group_id):
    """Enable only services reachable from non-disabled processors, dependencies first."""
    services, processors = {}, []
    def visit(gid):
        flow = client.get(f"/flow/process-groups/{gid}")["processGroupFlow"]["flow"]
        processors.extend(p["component"] for p in flow.get("processors", [])
                          if p.get("component", {}).get("state") != "DISABLED")
        entities = client.get(f"/flow/process-groups/{gid}/controller-services").get("controllerServices", [])
        services.update({s["id"]: s for s in entities})
        for g in flow.get("processGroups", []):
            visit(g["id"])
    visit(group_id)
    done, visiting = set(), set()
    def references(component):
        return [v for v in (component.get("properties") or component.get("config", {}).get("properties") or {}).values()
                if isinstance(v, str) and v in services]
    def enable(sid):
        if sid in done:
            return
        if sid in visiting:
            raise ValueError("Controller service dependency cycle")
        visiting.add(sid)
        for dependency in references(services[sid]["component"]):
            enable(dependency)
        entity = client.get("/controller-services/" + api_path(sid))
        if entity["component"].get("state") != "ENABLED":
            client.request("PUT", f"/controller-services/{sid}/run-status",
                           {"revision": entity["revision"], "state": "ENABLED"})
            deadline = time.monotonic() + 120
            while client.get("/controller-services/" + sid)["component"].get("state") != "ENABLED":
                if time.monotonic() >= deadline:
                    raise ApiError("Controller service did not enable; inspect target configuration")
                time.sleep(1)
        visiting.remove(sid)
        done.add(sid)
    for processor in processors:
        for sid in references(processor):
            enable(sid)


def apply_parameters(client, values):
    known = {}
    for entity in client.get("/flow/parameter-contexts").get("parameterContexts", []):
        component = entity.get("component", {})
        known.setdefault(component.get("name"), []).append(entity["id"])
    for name, parameters in values.items():
        ids = known.get(name, [])
        if len(ids) != 1:
            raise ValueError(f"Parameter context is missing or ambiguous: {name}")
        cid = ids[0]
        entity = client.get("/parameter-contexts/" + api_path(cid))
        component = entity["component"]
        existing = {p["parameter"]["name"]: p["parameter"] for p in component.get("parameters", [])}
        updates = []
        for key, value in parameters.items():
            if key not in existing:
                raise ValueError(f"Unknown parameter {name}/{key}")
            if value is None:
                raise ValueError("Null overrides are not accepted; omit a key to preserve its value")
            updates.append({"parameter": {"name": key, "sensitive": existing[key].get("sensitive", False), "value": str(value)}})
        if updates:
            payload = {"revision": entity["revision"], "component": {"id": cid, "name": name, "parameters": updates}}
            result = client.request("POST", f"/parameter-contexts/{api_path(cid)}/update-requests", payload)
            rid = result["request"]["requestId"]
            client.poll(f"/parameter-contexts/{api_path(cid)}/update-requests/{api_path(rid)}")


def apply_components(client, root_id, overrides):
    """Apply direct properties, including secrets not parameterized in the source."""
    for override in overrides:
        gid = root_id
        for name in override.get("groups", []):
            flow = client.get(f"/flow/process-groups/{gid}")["processGroupFlow"]["flow"]
            matches = [g for g in flow.get("processGroups", []) if g["component"]["name"] == name]
            if len(matches) != 1:
                raise ValueError("Component override group path is missing or ambiguous")
            gid = matches[0]["id"]
        kind = override.get("kind")
        if kind == "processor":
            candidates = client.get(f"/flow/process-groups/{gid}")["processGroupFlow"]["flow"].get("processors", [])
            resource = "processors"
        elif kind == "controller-service":
            candidates = client.get(f"/flow/process-groups/{gid}/controller-services").get("controllerServices", [])
            candidates = [s for s in candidates if s["component"].get("parentGroupId") == gid]
            resource = "controller-services"
        else:
            raise ValueError("Component override kind must be processor or controller-service")
        matches = [p for p in candidates if p["component"]["name"] == override["name"]]
        if len(matches) != 1:
            raise ValueError("Component override name is missing or ambiguous")
        entity = client.get(f"/{resource}/{matches[0]['id']}")
        if entity["component"].get("state") not in ("STOPPED", "DISABLED"):
            raise ValueError("Stop/disable the target component before overriding its properties")
        properties = override["properties"]
        if not properties or any(value is None for value in properties.values()):
            raise ValueError("Component overrides require non-null property values")
        properties = {k: str(v) for k, v in properties.items()}
        change = {"config": {"properties": properties}} if kind == "processor" else {"properties": properties}
        client.request("PUT", f"/{resource}/{entity['id']}",
                       {"revision": entity["revision"], "component": {"id": entity["id"], **change}})


def verify_live(client, group_id, expected, snapshot=None):
    counts = {"processors": 0, "controllerServices": 0, "connections": 0, "processGroups": 0,
              "inputPorts": 0, "outputPorts": 0}
    invalid = []
    mismatches = []
    running = 0
    def visit(gid, source_group=None):
        nonlocal running
        counts["processGroups"] += 1
        flow = client.get("/flow/process-groups/" + api_path(gid))["processGroupFlow"]["flow"]
        for key in ("processors", "connections", "inputPorts", "outputPorts"):
            counts[key] += len(flow.get(key, []))
        for p in flow.get("processors", []):
            comp = p.get("component", {})
            source = select_source(comp, source_group.get("processors", [])) if source_group else None
            if source:
                if source.get("scheduledState") == "DISABLED" and comp.get("state") != "DISABLED":
                    client.request("PUT", f"/processors/{p['id']}/run-status",
                                   {"revision": p["revision"], "state": "DISABLED"})
                    comp["state"] = "DISABLED"
                if comp.get("type") != source.get("type") or comp.get("bundle") != source.get("bundle"):
                    mismatches.append({"id": p["id"], "expectedType": source.get("type"),
                                       "actualType": comp.get("type"), "expectedDisabled": source.get("scheduledState") == "DISABLED"})
            running += comp.get("state") == "RUNNING"
            if comp.get("validationStatus") == "INVALID" and comp.get("state") != "DISABLED":
                invalid.append({"id": p["id"], "type": comp.get("type"), "name": comp.get("name")})
        # Endpoint includes ancestor services: count only services owned by this group.
        services = client.get("/flow/process-groups/" + api_path(gid) + "/controller-services").get("controllerServices", [])
        owned = [s for s in services if s.get("component", {}).get("parentGroupId") == gid]
        counts["controllerServices"] += len(owned)
        for service in owned if source_group else []:
            comp = service["component"]
            original = select_source(comp, source_group.get("controllerServices", []))
            if comp.get("type") != original.get("type") or comp.get("bundle") != original.get("bundle"):
                mismatches.append({"id": service["id"], "expectedType": original.get("type"), "actualType": comp.get("type")})
        for child in flow.get("processGroups", []):
            expected_child = select_source(child["component"], source_group.get("processGroups", [])) if source_group else None
            visit(child["id"], expected_child)
    visit(group_id, snapshot["flowContents"] if snapshot else None)
    if snapshot and snapshot.get("parameterContexts"):
        contexts = snapshot["parameterContexts"]
        expected_names = {c["name"] for c in (contexts.values() if isinstance(contexts, dict) else contexts)}
        actual_names = [c.get("component", {}).get("name") for c in client.get("/flow/parameter-contexts").get("parameterContexts", [])]
        if any(actual_names.count(name) != 1 for name in expected_names):
            raise ValueError("Imported parameter context is missing or ambiguous")
        counts["parameterContexts"] = len(expected_names)
    if any(counts[k] != expected.get(k, 0) for k in counts):
        raise RuntimeError("Imported component counts differ from the exported bundle")
    return {"counts": counts, "runningProcessors": running, "invalidProcessors": invalid, "typeMismatches": mismatches,
            "queue": status(client, group_id).get("flowFilesQueued", 0)}


def deploy(bundle, env_file, parameters_file=None, overwrite=False, start=False, timeout=600, component_file=None):
    bundle = Path(bundle).resolve()
    release = validate_bundle(bundle)
    config = load_env(env_file)
    required = ("COMPOSE_PROJECT_NAME", "DEPLOY_DIRECTORY", "NGINX_USERNAME", "NGINX_PASSWORD")
    if any(not config.get(k) for k in required):
        raise ValueError("Missing required local setting: " + ", ".join(k for k in required if not config.get(k)))
    project = config["COMPOSE_PROJECT_NAME"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ValueError("Invalid Compose project name")
    username = config["NGINX_USERNAME"]
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", username):
        raise ValueError("Invalid Basic Auth username")
    port = int(config.get("HTTP_PORT", "9080"))
    bind = config.get("BIND_ADDRESS", "127.0.0.1")
    if bind not in ("127.0.0.1", "0.0.0.0") or not 1024 <= port <= 65535:
        raise ValueError("Use BIND_ADDRESS=127.0.0.1 or 0.0.0.0 and port 1024..65535")
    target = Path(config["DEPLOY_DIRECTORY"]).expanduser().resolve()
    state_path = target / "deployment.json"
    owner_path = target / "owner.json"
    state = read_json(state_path) if state_path.exists() else None
    owner = read_json(owner_path) if owner_path.exists() else None
    if owner and (owner.get("project") != project or owner.get("name") != release["name"]):
        raise ValueError("Deployment directory is owned by another application")
    if target.exists() and any(target.iterdir()) and not owner:
        raise ValueError("Deployment directory is not empty and is not managed by this kit")
    if state and (state.get("project") != project or state.get("name") != release["name"]):
        raise ValueError("Deployment directory belongs to another application/project")
    env = dict(os.environ)
    env.update({"COMPOSE_PROJECT_NAME": project, "RUNTIME_DIRECTORY": str(target),
                "NIFI_IMAGE": release["nifiImage"], "NGINX_IMAGE": release["nginxImage"],
                "REGISTRY_IMAGE": release["registryImage"], "HTTP_PORT": str(port), "BIND_ADDRESS": bind,
                "NIFI_JVM_HEAP_INIT": config.get("NIFI_JVM_HEAP_INIT", "1g"),
                "NIFI_JVM_HEAP_MAX": config.get("NIFI_JVM_HEAP_MAX", "2g")})
    compose = ["docker", "compose", "--project-name", project, "--file", str(ROOT / "infrastructure/compose.yml")]
    if release.get("registryEnabled"):
        compose += ["--profile", "registry"]
    run(["docker", "compose", "version"])
    existing = run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=" + project, "--format", "{{.ID}}"])
    if existing.strip() and not owner:
        raise ValueError("Compose project already exists without this deployment state; refusing adoption")
    if state and state.get("nifiVersion") != release["nifiVersion"]:
        raise ValueError("NiFi upgrades require a separate migration; existing version differs")
    if state and state.get("flowSha256") != release["flowSha256"] and not overwrite:
        raise ValueError("Flow changed; use --overwrite-config after preparing/draining the target")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(owner_path, {"project": project, "name": release["name"]}, private=True)
    for folder in ("nginx", "lib", "extensions", "scripts", "data"):
        (target / folder).mkdir(exist_ok=True)
    for artifact in release.get("artifacts", []):
        destination = target / artifact["target"] / Path(artifact["file"]).name
        if not destination.exists() or digest(destination) != artifact["sha256"]:
            shutil.copyfile(bundle / artifact["file"], destination)
    shutil.copyfile(ROOT / "infrastructure/start-http.sh", target / "start-http.sh")
    (target / "start-http.sh").chmod(0o755)
    shutil.copyfile(ROOT / "infrastructure/nginx.conf", target / "nginx/nifi.conf")
    auth = run(["docker", "run", "--rm", "-i", "httpd:2.4-alpine", "htpasswd", "-niB", username],
               data=config["NGINX_PASSWORD"] + "\n")
    (target / "nginx/.htpasswd").write_text(auth)
    (target / "nginx/.htpasswd").chmod(0o644)
    run(compose + ["up", "-d"], env=env)
    client = Client(f"http://127.0.0.1:{port}", username, config["NGINX_PASSWORD"])
    about = client.wait_ready(timeout)
    if about.get("version") != release["nifiVersion"]:
        raise ValueError("Running NiFi version does not match the bundle")
    if release.get("registryEnabled"):
        registry_id = run(compose + ["ps", "-q", "registry"], env=env).strip()
        deadline = time.monotonic() + 180
        while True:
            health = json.loads(run(["docker", "inspect", "--format", "{{json .State.Health}}", registry_id]))
            if health and health.get("Status") == "healthy":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Registry API did not become healthy; inspect its local logs")
            time.sleep(3)
    root_id = client.root_id()
    snapshot = read_json(bundle / release["flowFile"])
    snapshot["flowContents"]["name"] = "nifi-deploy-kit-" + release["name"]
    if state:
        group_id = state["groupId"]
        client.get("/process-groups/" + api_path(group_id))  # Fail if state points to a removed group.
        if overwrite or state.get("flowSha256") != release["flowSha256"]:
            before = status(client, group_id)
            current = client.get("/process-groups/" + api_path(group_id))
            if before.get("flowFilesQueued", 0) or before.get("activeThreadCount", 0) or current.get("runningCount", 0):
                raise ValueError("Target has queued data/active tasks; drain before replacing the flow")
            write_json(target / "backup-flow.json", client.get(f"/process-groups/{group_id}/download?includeReferencedServices=true&includeComponentState=false"), private=True)
            result = client.request("POST", f"/process-groups/{group_id}/replace-requests",
                                    {"processGroupRevision": current["revision"], "versionedFlowSnapshot": snapshot})
            rid = result["request"]["requestId"]
            client.poll("/process-groups/replace-requests/" + api_path(rid), timeout)
    else:
        flow = client.get("/flow/process-groups/" + api_path(root_id))["processGroupFlow"]["flow"]
        matches = [g for g in flow.get("processGroups", [])
                   if g.get("component", {}).get("name") == snapshot["flowContents"]["name"]]
        if len(matches) > 1:
            raise ValueError("Multiple matching imported groups; resolve ambiguity in the target UI")
        if matches:
            group_id = matches[0]["id"]
        else:
            current = client.get("/process-groups/" + api_path(root_id))
            result = client.request("POST", f"/process-groups/{root_id}/process-groups/import", import_payload(snapshot))
            group_id = result["id"]
        # Record ID immediately: a later failed parameter application must not duplicate the import.
        state = {"project": project, "name": release["name"], "nifiVersion": release["nifiVersion"],
                 "groupId": group_id, "flowSha256": release["flowSha256"]}
        write_json(state_path, state, private=True)
    if parameters_file:
        apply_parameters(client, read_json(parameters_file))
    if component_file:
        apply_components(client, group_id, read_json(component_file))
    check = verify_live(client, group_id, release["counts"], snapshot)
    if start:
        if check["typeMismatches"]:
            raise ValueError("Imported component type/bundle differs from source; inspect target dependency/migration before --start")
        enable_referenced_services(client, group_id)
        check = verify_live(client, group_id, release["counts"], snapshot)
        if check["invalidProcessors"]:
            raise ValueError("Flow has invalid processors; supply missing parameters/dependencies before --start")
        client.request("PUT", f"/flow/process-groups/{group_id}", {"id": group_id, "state": "RUNNING"})
    elif not state.get("started") and check["runningProcessors"]:
        client.request("PUT", f"/flow/process-groups/{group_id}", {"id": group_id, "state": "STOPPED"})
        raise RuntimeError("Imported processors unexpectedly started; stopped target group for inspection")
    state.update({"flowSha256": release["flowSha256"], "started": state.get("started", False) or start})
    write_json(state_path, state, private=True)
    report = {"project": project, "bundle": release["name"], "nifiVersion": about["version"],
              "url": f"http://localhost:{port}/nifi/", "groupId": group_id,
              "flowSha256": release["flowSha256"], "mode": "running" if start or check["runningProcessors"] else "staged",
              "registryHealthy": True if release.get("registryEnabled") else None,
              "configurationComplete": not check["invalidProcessors"] and not check["typeMismatches"], **check}
    write_json(target / "report.json", report, private=True)
    return report
