import copy
import unittest

from nifi_deploy.api import ApiError, Client
from nifi_deploy.bundle import prepare_snapshot, summary
from nifi_deploy.deployment import apply_parameters, import_payload, enable_referenced_services, verify_live


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


if __name__ == "__main__":
    unittest.main()
