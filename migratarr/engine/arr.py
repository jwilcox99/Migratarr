"""Radarr/Sonarr HTTP client and the Arr-side file checks, one copy for every executor.

Each executor gets its own subclass (client('radarr', names)) whose `ns` is
that executor's globals, read at use time: RUNTIME (URLs, secrets,
arr_file_checks), TARGETS (arr_root) and run_progress. So a patched or
reloaded runtime is honored exactly as when the class was pasted into each
executor, and the commands it runs are unchanged (tests/test_arr_file_checks.py).
"""
import json
import subprocess
import urllib.request

from migratarr.arr_files import host_path, host_sha256, host_visible
from migratarr.engine.nas_side import require
from migratarr.service_keys import SecretError, service_endpoint


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Disable redirects so an API key cannot be forwarded to another server.
    def redirect_request(self, *args, **kwargs):
        return None


class ArrClient:
    service = None  # 'radarr' or 'sonarr'
    ns = None       # the executor's names (migratarr.engine.run.Names)

    @property
    def label(self):
        return self.service.capitalize()

    def __init__(self):
        # runtime.json urls + "secrets" (default: ApiKey/UrlBase from the container's config.xml).
        try:
            self.api_root, self.key = service_endpoint(self.service, self.ns.RUNTIME)
        except SecretError as exc:
            require(False, str(exc))
        require(self.key, self.label + ' API key missing')
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def api(self, path, body=None):
        req = urllib.request.Request(self.api_root + '/api/v3/' + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
                                     method='GET' if body is None else 'PUT')
        with self.opener.open(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    def visible(self, path, kind='-f'):
        # runtime.json arr_file_checks (default docker; see arr_files.py).
        check = self.ns.RUNTIME.arr_file_checks[self.service]
        if check['mode'] == 'host':
            require(host_visible(host_path(check, self.ns.TARGETS.arr_root, path), kind),
                    self.label + ' path not visible on this host: ' + path)
            return
        r = subprocess.run(['docker', 'exec', check['container'], 'test', kind, path], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        require(r.returncode == 0, self.label + ' container cannot see required path: ' + path)

    def verify_file(self, path, expected_hash):
        self.visible(path)
        check = self.ns.RUNTIME.arr_file_checks[self.service]
        if check['mode'] == 'host':
            digest = host_sha256(host_path(check, self.ns.TARGETS.arr_root, path))
            require(digest == expected_hash, self.label + '-visible file content mismatch')
            return
        result = self.ns.run_progress(['docker', 'exec', check['container'], 'sha256sum', '--', path],
                                      self.label + ' file-content verification')
        require(result.split()[0] == expected_hash, self.label + '-visible file content mismatch')


def client(service, ns):
    """The executor's Radarr or Sonarr class, bound to its names."""
    name = service.capitalize()
    return type(name, (ArrClient,), dict(service=service, ns=ns, __module__=ns.__name__, __qualname__=name))
