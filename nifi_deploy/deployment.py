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


def verify_live(client, group_id, expected):
    counts = {"processors": 0, "controllerServices": 0, "connections": 0, "processGroups": 0}
    invalid = []
    running = 0
    def visit(gid):
        nonlocal running
        counts["processGroups"] += 1
        flow = client.get("/flow/process-groups/" + api_path(gid))["processGroupFlow"]["flow"]
        for key in ("processors", "connections"):
            counts[key] += len(flow.get(key, []))
        for p in flow.get("processors", []):
            comp = p.get("component", {})
            running += comp.get("state") == "RUNNING"
            if comp.get("validationStatus") == "INVALID" and comp.get("state") != "DISABLED":
                invalid.append({"id": p["id"], "type": comp.get("type"), "name": comp.get("name")})
        # Endpoint includes ancestor services: count only services owned by this group.
        services = client.get("/flow/process-groups/" + api_path(gid) + "/controller-services").get("controllerServices", [])
        counts["controllerServices"] += sum(s.get("component", {}).get("parentGroupId") == gid for s in services)
        for child in flow.get("processGroups", []):
            visit(child["id"])
    visit(group_id)
    if any(counts[k] != expected.get(k, 0) for k in counts):
        raise RuntimeError("Imported component counts differ from the exported bundle")
    return {"counts": counts, "runningProcessors": running, "invalidProcessors": invalid,
            "queue": status(client, group_id).get("flowFilesQueued", 0)}


def deploy(bundle, env_file, parameters_file=None, overwrite=False, start=False, timeout=600):
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
        shutil.copyfile(bundle / artifact["file"], target / artifact["target"] / Path(artifact["file"]).name)
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
    root_id = client.root_id()
    snapshot = read_json(bundle / release["flowFile"])
    snapshot["flowContents"]["name"] = "nifi-deploy-kit-" + release["name"]
    if state:
        group_id = state["groupId"]
        client.get("/process-groups/" + api_path(group_id))  # Fail if state points to a removed group.
        if state.get("flowSha256") != release["flowSha256"]:
            before = status(client, group_id)
            if before.get("flowFilesQueued", 0) or before.get("activeThreadCount", 0):
                raise ValueError("Target has queued data/active tasks; drain before replacing the flow")
            current = client.get("/process-groups/" + api_path(group_id))
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
            result = client.request("POST", f"/process-groups/{root_id}/process-groups/import",
                                    {"processGroupRevision": current["revision"], "versionedFlowSnapshot": snapshot})
            group_id = result["id"]
        # Record ID immediately: a later failed parameter application must not duplicate the import.
        state = {"project": project, "name": release["name"], "nifiVersion": release["nifiVersion"],
                 "groupId": group_id, "flowSha256": release["flowSha256"]}
        write_json(state_path, state, private=True)
    if parameters_file:
        apply_parameters(client, read_json(parameters_file))
    check = verify_live(client, group_id, release["counts"])
    if start:
        services = client.get(f"/flow/process-groups/{group_id}/controller-services").get("controllerServices", [])
        if services:
            client.request("PUT", f"/flow/process-groups/{group_id}/controller-services",
                           {"id": group_id, "state": "ENABLED"})
            time.sleep(3)
        check = verify_live(client, group_id, release["counts"])
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
              "flowSha256": release["flowSha256"], "mode": "running" if start else "staged",
              "configurationComplete": not check["invalidProcessors"], **check}
    write_json(target / "report.json", report, private=True)
    return report
