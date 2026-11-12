import http.client
import io
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from nifi_deploy.api import ApiError, Client


class ApiReadinessTests(unittest.TestCase):
    def test_startup_transport_errors_and_unready_responses_are_retried(self):
        client = Client('http://localhost:9080/nifi')
        malformed = MagicMock()
        malformed.__enter__.return_value.read.return_value = b'<html>starting</html>'
        ready = MagicMock()
        ready.__enter__.return_value.read.return_value = b'{"about":{"version":"2.12.0"}}'
        client.opener = MagicMock()
        client.opener.open.side_effect = [
            ConnectionResetError('private-marker'),
            http.client.RemoteDisconnected('private-marker'),
            urllib.error.HTTPError(client.url, 503, 'private-marker', {}, None),
            malformed, ready]
        clock = [0.0]
        output = io.StringIO()
        with patch('nifi_deploy.api.time.monotonic', side_effect=lambda: clock[0]), \
             patch('nifi_deploy.api.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch('sys.stderr', output):
            self.assertEqual(client.wait_ready(60)['version'], '2.12.0')
        self.assertEqual(client.opener.open.call_count, 5)
        self.assertTrue(all(call.args[0].method == 'GET' for call in client.opener.open.call_args_list))
        self.assertIn('Waiting for NiFi API', output.getvalue())
        self.assertNotIn('private-marker', output.getvalue())

    def test_readiness_timeout_bounds_requests_and_waits(self):
        client = Client('http://localhost:9080', timeout=120)
        client.opener = MagicMock()
        client.opener.open.side_effect = ConnectionResetError('private-marker')
        clock = [0.0]
        with patch('nifi_deploy.api.time.monotonic', side_effect=lambda: clock[0]), \
             patch('nifi_deploy.api.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(ApiError, 'before the timeout'):
                client.wait_ready(5)
        self.assertEqual(clock[0], 5)
        self.assertEqual([c.kwargs['timeout'] for c in client.opener.open.call_args_list], [5, 2])

    def test_mutating_requests_are_not_replayed_after_transport_failure(self):
        for error in (ConnectionResetError('private-marker'),
                      http.client.IncompleteRead(b'private-marker', 30)):
            with self.subTest(error=type(error).__name__):
                client = Client('http://localhost:9080')
                client.opener = MagicMock()
                client.opener.open.side_effect = error
                with self.assertRaises(ApiError) as raised:
                    client.request('POST', '/process-groups/root/process-groups/import', {'secret': 'private-marker'})
                self.assertEqual(client.opener.open.call_count, 1)
                self.assertNotIn('private-marker', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
