import subprocess
import sys
import time

from .api import ApiError, api_path


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

def verify_live(client, group_id, expected, snapshot=None):
    counts = {"processors": 0, "controllerServices": 0, "connections": 0, "processGroups": 0,
              "inputPorts": 0, "outputPorts": 0}
    invalid = []
    mismatches = []
    validating = []
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
            elif comp.get("validationStatus") not in ("VALID", "INVALID") and comp.get("state") != "DISABLED":
                validating.append({"id": p["id"], "kind": "processor", "name": comp.get("name")})
        # Endpoint includes ancestor services: count only services owned by this group.
        services = client.get("/flow/process-groups/" + api_path(gid) + "/controller-services").get("controllerServices", [])
        owned = [s for s in services if s.get("component", {}).get("parentGroupId") == gid]
        counts["controllerServices"] += len(owned)
        for service in owned:
            comp = service["component"]
            if comp.get("validationStatus") == "VALIDATING" and not comp.get("extensionMissing"):
                validating.append({"id": service["id"], "kind": "controller-service", "name": comp.get("name")})
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
            "validatingComponents": validating,
            "queue": status(client, group_id).get("flowFilesQueued", 0)}


def wait_for_validation(client, group_id, expected, snapshot=None, seconds=600):
    """Wait for two settled validation readings; never treat VALIDATING as valid."""
    deadline = time.monotonic() + seconds
    settled = None
    waiting = False
    while True:
        result = verify_live(client, group_id, expected, snapshot)
        signature = (tuple(sorted(p['id'] for p in result['invalidProcessors'])),
                     tuple(sorted(p['id'] for p in result['typeMismatches'])))
        if not result['validatingComponents']:
            if settled == signature:
                return result
            settled = signature
        else:
            settled = None
            if not waiting:
                print("Waiting for NiFi component validation...", file=sys.stderr, flush=True)
                waiting = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApiError("NiFi component validation did not finish before the timeout; inspect the target UI")
        time.sleep(min(1, remaining))
