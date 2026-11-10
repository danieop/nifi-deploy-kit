import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nifi_deploy.api import ApiError, Client
from nifi_deploy.bundle import prepare_snapshot, summary, validate_bundle, write_json, digest
from nifi_deploy.deployment import apply_parameters, load_env, import_payload, enable_referenced_services, verify_live


SNAPSHOT = {
    "flowContents": {"name": "test", "processors": [
        {"type": "example.P", "scheduledState": "RUNNING", "bundle": {"group": "test", "artifact": "nar", "version": "1"}},
        {"type": "example.P", "scheduledState": "DISABLED"}],
        "controllerServices": [{"scheduledState": "ENABLED"}],
        "processGroups": [{"processors": [], "connections": [{}]}]},
    "parameterContexts": {"p": {"name": "p", "parameters": [
        {"name": "password", "sensitive": True, "value": "private"},
        {"name": "host", "sensitive": False, "value": "example.internal"}]}}}


class BundleTests(unittest.TestCase):
    def test_export_stages_processors_and_preserves_disabled(self):
        original = copy.deepcopy(SNAPSHOT)
        prepared = prepare_snapshot(SNAPSHOT)
        self.assertEqual([p["scheduledState"] for p in prepared["flowContents"]["processors"]], ["ENABLED", "DISABLED"])
        self.assertEqual(SNAPSHOT, original)
        self.assertNotIn("value", prepared["parameterContexts"]["p"]["parameters"][0])
        self.assertEqual(prepared["parameterContexts"]["p"]["parameters"][1]["value"], "example.internal")

    def test_recursive_component_counts(self):
        counts = summary(SNAPSHOT)["counts"]
        self.assertEqual(counts["processors"], 2)
        self.assertEqual(counts["processGroups"], 2)
        self.assertEqual(counts["connections"], 1)

    def test_checksum_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(Path(d) / "flow.json", prepare_snapshot(SNAPSHOT))
            release = {"schemaVersion": 1, "flowFile": "flow.json", "flowSha256": digest(Path(d) / "flow.json")}
            write_json(Path(d) / "release.json", release)
            validate_bundle(d)
            (Path(d) / "flow.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "checksum"):
                validate_bundle(d)

    def test_artifact_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(Path(d) / "flow.json", prepare_snapshot(SNAPSHOT))
            write_json(Path(d) / "release.json", {"schemaVersion": 1, "flowFile": "flow.json", "flowSha256": digest(Path(d) / "flow.json"),
                       "artifacts": [{"file": "../secret", "target": "lib", "sha256": "x"}]})
            with self.assertRaisesRegex(ValueError, "artifact"):
                validate_bundle(d)

    def test_env_is_literal_not_shell_execution(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "env"
            path.write_text('PASSWORD="a$HOME!#z"\nVALUE=$(touch /not-executed)\n')
            self.assertEqual(load_env(path), {"PASSWORD": "a$HOME!#z", "VALUE": "$(touch /not-executed)"})

    def test_null_parameter_override_is_not_silently_ignored(self):
        class Fake:
            def get(self, path):
                if path.startswith("/flow/"):
                    return {"parameterContexts": [{"id": "p", "component": {"name": "p"}}]}
                return {"revision": {"version": 1}, "component": {"parameters": [{"parameter": {"name": "secret", "sensitive": True}}]}}
        with self.assertRaisesRegex(ValueError, "Null"):
            apply_parameters(Fake(), {"p": {"secret": None}})

    def test_url_normalization(self):
        self.assertEqual(Client("http://localhost:9080/nifi/").url, "http://localhost:9080/nifi-api")

    def test_import_uses_upload_entity_not_replace_entity(self):
        payload = import_payload(SNAPSHOT)
        self.assertIn("flowSnapshot", payload)
        self.assertNotIn("versionedFlowSnapshot", payload)
        self.assertEqual(payload["revisionDTO"]["version"], 0)

    def test_snapshot_run_states_are_native_not_rest_run_states(self):
        prepared = prepare_snapshot(SNAPSHOT)
        self.assertEqual(prepared["flowContents"]["scheduledState"], "ENABLED")
        self.assertEqual(prepared["flowContents"]["controllerServices"][0]["scheduledState"], "DISABLED")

    def test_only_referenced_services_enable_dependency_first(self):
        class Fake:
            enabled = []
            def get(self, path):
                services = [{"id": x, "component": {"properties": {"dependency": "b"} if x == "a" else {}}}
                            for x in ["unused", "a", "b"]]
                if path.endswith("/controller-services"):
                    return {"controllerServices": services}
                if path.startswith("/flow/"):
                    return {"processGroupFlow": {"flow": {"processors": [
                        {"component": {"state": "STOPPED", "config": {"properties": {"pool": "a"}}}},
                        {"component": {"state": "DISABLED", "config": {"properties": {"pool": "unused"}}}}]}}}
                return {"revision": {"version": 0}, "component": {"state": "ENABLED" if path.rsplit("/", 1)[-1] in self.enabled else "DISABLED"}}
            def request(self, method, path, payload):
                self.enabled.append(path.split("/")[2])
        client = Fake()
        enable_referenced_services(client, "root")
        self.assertEqual(client.enabled, ["b", "a"])

    def test_verification_reports_type_changes_and_restores_disabled(self):
        class Fake:
            requests = []
            def get(self, path):
                if path.endswith('/status'):
                    return {"processGroupStatus": {"aggregateSnapshot": {"flowFilesQueued": 0}}}
                if path.endswith('/controller-services'):
                    return {"controllerServices": []}
                return {"processGroupFlow": {"flow": {"processors": [{"id": "actual", "revision": {"version": 0},
                    "component": {"versionedComponentId": "original", "type": "Changed", "state": "STOPPED"}}]}}}
            def request(self, method, path, payload):
                self.requests.append(payload["state"])
        client = Fake()
        expected = {"processors": 1, "controllerServices": 0, "connections": 0, "processGroups": 1}
        snapshot = {"flowContents": {"processors": [{"identifier": "original", "type": "Original", "scheduledState": "DISABLED"}]}}
        result = verify_live(client, "group", expected, snapshot)
        self.assertEqual(client.requests, ["DISABLED"])
        self.assertEqual(len(result["typeMismatches"]), 1)

    def test_export_uses_only_get(self):
        from nifi_deploy.bundle import export_bundle
        class Fake:
            calls = []
            def root_id(self):
                return "root"
            def get(self, path):
                self.calls.append(path)
                if path == "/flow/about":
                    return {"about": {"version": "2.12.0"}}
                if path.startswith("/process-groups/"):
                    self.assertion = "includeComponentState=false" in path
                    return SNAPSHOT
                return {"registries": []}
        client = Fake()
        with tempfile.TemporaryDirectory() as d:
            export_bundle(client, d, "test")
            self.assertTrue(client.assertion)
            validate_bundle(d)


if __name__ == "__main__":
    unittest.main()
