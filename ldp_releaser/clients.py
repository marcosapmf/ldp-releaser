"""Small Jenkins and Jira REST clients built on the standard library."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class Jenkins:

    def __init__(self, base_url, folder, token, user):
        self.base_url = base_url.rstrip("/")
        self._authorization = "Basic " + base64.b64encode(
            f"{user}:{token}".encode()).decode()
        self._folder_path = "".join(
            f"/job/{part}" for part in folder.strip("/").split("/") if part)

    def check(self):

        # Raises when the login is rejected or the folder is not visible.

        self.get_json(
            f"{self.base_url}{self._folder_path}/api/json", "name")

    def get_json(self, url, tree=None):
        if tree:
            url += "?" + urllib.parse.urlencode({"tree": tree})

        with self._open(url) as response:
            return json.load(response)

    def get_text(self, url):
        with self._open(url) as response:
            return response.read().decode("utf-8", "replace")

    def job_url(self, job):
        return f"{self.base_url}{self._folder_path}/job/{job}/"

    def post(self, url, params=None):
        with self._open(url, params, post=True):
            pass

    def read_log(self, build_url, start):
        with self._open(
                f"{build_url}logText/progressiveText?start={start}") as response:
            text = response.read().decode("utf-8", "replace")

            return text, int(response.headers.get("X-Text-Size", start))

    def trigger(self, job, params):
        with self._open(
                self.job_url(job) + "buildWithParameters", params,
                post=True) as response:
            return response.headers["Location"]

    def _open(self, url, params=None, post=False):
        body = None

        if post:
            body = urllib.parse.urlencode(params or {}).encode()

        request = urllib.request.Request(
            url, data=body, headers={"Authorization": self._authorization},
            method="POST" if post else "GET")

        return urllib.request.urlopen(request, timeout=60)


class Jira:

    def __init__(self, base_url, token, user):
        self.base_url = base_url.rstrip("/")
        self._authorization = "Basic " + base64.b64encode(
            f"{user}:{token}".encode()).decode()

    def check(self):

        # Returns the account behind the token, or raises when rejected.

        return self.request("GET", "/rest/api/3/myself")

    def issue_url(self, key):
        return f"{self.base_url}/browse/{key}"

    def request(self, method, path, payload=None):
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization,
        }

        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content = response.read()
        except urllib.error.HTTPError as exception:
            detail = exception.read().decode("utf-8", "replace")

            raise RuntimeError(
                f"Jira {method} {path} failed ({exception.code}): "
                f"{detail[:500]}") from exception

        if not content:
            return None

        return json.loads(content)

    def status(self, key):
        return self.request("GET", f"/rest/api/3/issue/{key}?fields=status")[
            "fields"]["status"]["name"]

    def transition_to(self, key, status_name, fields=None):
        transitions = self.request(
            "GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]

        for transition in transitions:
            if transition["to"]["name"] == status_name:
                payload = {"transition": {"id": transition["id"]}}

                if fields:
                    payload["fields"] = fields

                self.request(
                    "POST", f"/rest/api/3/issue/{key}/transitions", payload)

                return

        available = ", ".join(
            transition["to"]["name"] for transition in transitions)

        raise RuntimeError(
            f"{key} has no transition to {status_name} (available: "
            f"{available})")
