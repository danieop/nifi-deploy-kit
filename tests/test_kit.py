import copy
import io
import unittest
from unittest.mock import patch

from nifi_deploy.api import ApiError, Client
from nifi_deploy.bundle import prepare_snapshot, summary
from nifi_deploy.deployment import apply_parameters, import_payload, enable_referenced_services, verify_live, wait_for_validation


SNAPSHOT = {
    "flowContents": {"name": "test", "processors": [
        {"type": "example.P", "scheduledState": "RUNNING", "bundle": {"group": "test", "artifact": "nar", "version": "1"}},
        {"type": "example.P", "scheduledState": "DISABLED"}],
        "controllerServices": [{"scheduledState": "ENABLED"}],
        "processGroups": [{"processors": [], "connections": [{}]}]},
    "parameterContexts": {"p": {"name": "p", "parameters": [
        {"name": "password", "sensitive": True, "value": "private"},
        {"name": "host", "sensitive": False, "value": "example.internal"}]}}}


class ValidationClient:
    def __init__(self, states, processor_state='STOPPED', service_states=None):
        self.states = states
        self.processor_state = processor_state
        self.service_states = service_states
        self.reads = 0

    def get(self, path):
        if path.endswith('/status'):
            return {'processGroupStatus': {'aggregateSnapshot': {'flowFilesQueued': 0}}}
        if path.endswith('/controller-services'):
            if self.service_states is None:
                return {'controllerServices': []}
            state = self.service_states[min(self.reads - 1, len(self.service_states) - 1)]
            return {'controllerServices': [{'id': 'service', 'component': {
                'parentGroupId': 'group', 'validationStatus': state, 'state': 'DISABLED'}}]}
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        return {'processGroupFlow': {'flow': {'processors': [{'id': 'processor', 'component': {
            'type': 'example.Processor', 'state': self.processor_state, 'validationStatus': state}}]}}}


VALIDATION_COUNTS = {'processors': 1, 'connections': 0, 'inputPorts': 0, 'outputPorts': 0,
                     'controllerServices': 0, 'processGroups': 1}


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

    def test_validation_wait_catches_late_invalid_result(self):
        client = ValidationClient(['VALIDATING', 'VALID', 'INVALID', 'INVALID'])
        with patch('nifi_deploy.deployment.time.sleep'), patch('sys.stderr', io.StringIO()):
            result = wait_for_validation(client, 'group', VALIDATION_COUNTS)
        self.assertEqual(client.reads, 4)
        self.assertEqual(len(result['invalidProcessors']), 1)
        self.assertEqual(result['validatingComponents'], [])

    def test_validation_wait_includes_owned_controller_services(self):
        client = ValidationClient(['VALID'], service_states=['VALIDATING', 'VALID', 'VALID'])
        with patch('nifi_deploy.deployment.time.sleep'), patch('sys.stderr', io.StringIO()):
            result = wait_for_validation(client, 'group', {**VALIDATION_COUNTS, 'controllerServices': 1})
        self.assertEqual(client.reads, 3)
        self.assertEqual(result['validatingComponents'], [])

    def test_pending_or_unknown_validation_cannot_be_reported_as_ready(self):
        for state in ('VALIDATING', None):
            with self.subTest(state=state):
                result = verify_live(ValidationClient([state]), 'group', VALIDATION_COUNTS)
                self.assertEqual(len(result['validatingComponents']), 1)

    def test_disabled_processors_do_not_block_validation(self):
        client = ValidationClient(['VALIDATING'], processor_state='DISABLED')
        with patch('nifi_deploy.deployment.time.sleep'):
            result = wait_for_validation(client, 'group', VALIDATION_COUNTS)
        self.assertEqual(result['validatingComponents'], [])
        self.assertEqual(client.reads, 2)

    def test_pending_validation_times_out_instead_of_returning_success(self):
        client = ValidationClient(['VALIDATING'])
        clock = [0.0]
        with patch('nifi_deploy.deployment.time.monotonic', side_effect=lambda: clock[0]), \
             patch('nifi_deploy.deployment.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(ApiError, 'validation did not finish'):
                wait_for_validation(client, 'group', VALIDATION_COUNTS, seconds=2)
        self.assertEqual(clock[0], 2)


if __name__ == "__main__":
    unittest.main()
