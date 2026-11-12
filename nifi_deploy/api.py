import base64
import http.client
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, url, username="", password="", ca_file=None, timeout=120):
        url = url.rstrip("/")
        if url.endswith("/nifi"):
            url = url[:-5]
        self.url = url if url.endswith("/nifi-api") else url + "/nifi-api"
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        if username:
            self.headers["Authorization"] = "Basic " + base64.b64encode(
                (username + ":" + password).encode()).decode()
        handlers = [urllib.request.ProxyHandler({})]
        if ca_file:
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method, path, payload=None, *, timeout=None):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            # Error bodies can contain submitted properties. Do not log them.
            raise ApiError(f"NiFi {method} {path.split('?')[0]}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            raise ApiError(f"NiFi {method} {path.split('?')[0]}: connection unavailable") from error

    def get(self, path, *, timeout=None):
        return self.request("GET", path, timeout=timeout)

    def root_id(self):
        return self.get("/flow/process-groups/root")["processGroupFlow"]["id"]

    def wait_ready(self, seconds=600):
        deadline = time.monotonic() + seconds
        waiting = False
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                about = self.get("/flow/about", timeout=min(self.timeout, remaining))["about"]
                if isinstance(about, dict) and about.get("version"):
                    if waiting:
                        print("NiFi API is ready.", file=sys.stderr, flush=True)
                    return about
            except (ApiError, json.JSONDecodeError, KeyError, TypeError):
                pass
            if not waiting:
                print("Waiting for NiFi API readiness...", file=sys.stderr, flush=True)
                waiting = True
            time.sleep(max(0, min(3, deadline - time.monotonic())))
        raise ApiError("NiFi did not become ready before the timeout")

    def poll(self, path, seconds=600):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            entity = self.get(path)
            request = entity.get("request", entity)
            if request.get("complete"):
                if request.get("failureReason"):
                    raise ApiError("NiFi asynchronous update failed; inspect the target UI")
                self.request("DELETE", path)
                return entity
            time.sleep(1)
        raise ApiError("NiFi asynchronous operation timed out; request retained for inspection")


def api_path(value):
    return urllib.parse.quote(str(value), safe="")
