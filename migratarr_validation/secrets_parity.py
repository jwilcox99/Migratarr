"""Compare the pre-service_keys credential lookups with service_keys, printing no secrets.

Each old lookup below is the code as it stood at 260746d, transcribed
verbatim (file:line cited) because most of it runs at module import time.
On a deployment this runs the real `docker exec` commands both ways and
reports only whether each pair of results is identical.
"""
import argparse
import hmac
import json
import os
import subprocess
import xml.etree.ElementTree as ET

SED_API_KEY = r"""sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml"""


def docker_output(container, command):
    """dry_run_movies.py:48-52, dry_run_tv.py:38-42 (identical)."""
    return subprocess.check_output(
        ["docker", "exec", container, "sh", "-c", command],
        text=True
    ).strip()


def old_planner_arr_key(container):
    """dry_run_movies.py get_radarr_key, dry_run_tv.py SONARR_KEY, build_move_plan.py
    docker_key and audit_overrides.py docker_key: the same sed over config.xml."""
    return docker_output(container, SED_API_KEY)


def old_movie_jellyfin_key(container):
    """dry_run_movies.py get_jellyfin_key."""
    paths = [
        "/run/secrets/jellyfin_api_key",
        "/run/secrets/jellyfin_key",
    ]
    for path in paths:
        try:
            value = docker_output(container, f"cat {path}")
            if value:
                return value
        except Exception:
            pass
    raise RuntimeError("Could not read Jellyfin key from Homepage secrets")


def old_tv_jellyfin_key(container):
    """dry_run_tv.py JELLYFIN_KEY."""
    return docker_output(container, "cat /run/secrets/jellyfin_api_key")


def old_executor_arr(container):
    """execute_cross_movie.py / execute_cross_tv.py / execute_tv_nas.py Radarr/Sonarr
    __init__ (key and UrlBase); execute_movie.py and execute_movie_nas.py read the
    key the same way and ignore UrlBase."""
    xml = subprocess.check_output(['docker', 'exec', container, 'cat', '/config/config.xml'], timeout=30)
    config = ET.fromstring(xml)
    key = config.findtext('ApiKey')
    url_base = (config.findtext('UrlBase') or '').rstrip('/')
    if not key:
        raise ValueError('API key missing')
    return key, url_base


def attempt(function, *args):
    try:
        return True, function(*args)
    except Exception as exc:
        return False, type(exc).__name__


def same(a, b):
    """Constant-time equality for secrets; plain equality for everything else."""
    if isinstance(a, str) and isinstance(b, str):
        return hmac.compare_digest(a.encode(), b.encode())
    return a == b


def compare(runtime):
    from service_keys import read_credential
    report = {}
    new = {service: attempt(read_credential, service, runtime) for service in ('radarr', 'sonarr', 'jellyfin', 'tmdb')}
    for service in ('radarr', 'sonarr'):
        container = runtime.containers[service]
        planner, executor, current = attempt(old_planner_arr_key, container), attempt(old_executor_arr, container), new[service]
        report[service] = {
            'old_planner_ok': planner[0], 'old_executor_ok': executor[0], 'new_ok': current[0],
            'planner_key_identical': planner[0] and current[0] and same(planner[1], current[1][0]),
            'executor_key_identical': executor[0] and current[0] and same(executor[1][0], current[1][0]),
            'url_base_identical': executor[0] and current[0] and executor[1][1] == current[1][1]}
    container = runtime.containers.get('homepage')
    movie, tv, current = attempt(old_movie_jellyfin_key, container), attempt(old_tv_jellyfin_key, container), new['jellyfin']
    report['jellyfin'] = {
        'old_movie_ok': movie[0], 'old_tv_ok': tv[0], 'new_ok': current[0],
        'movie_key_identical': movie[0] and current[0] and same(movie[1], current[1][0]),
        'tv_key_identical': tv[0] and current[0] and same(tv[1], current[1][0])}
    old_tmdb = os.environ.get('TMDB_TOKEN')  # dry_run_*.py: os.environ["TMDB_TOKEN"]
    if old_tmdb:
        report['tmdb'] = {'old_ok': True, 'new_ok': new['tmdb'][0],
                          'identical': new['tmdb'][0] and same(old_tmdb.strip(), new['tmdb'][1][0])}
    else:
        report['tmdb'] = {'skipped': 'TMDB_TOKEN not set, so there is no old value to compare',
                          'new_ok': new['tmdb'][0]}
    # API roots (service_endpoint) vs the URL each old call site built: the planners,
    # build_move_plan, audit_overrides, execute_movie and execute_movie_nas used
    # urls[service] as-is; the other three executors appended the config.xml UrlBase.
    from service_keys import service_endpoint
    for service in ('radarr', 'sonarr', 'jellyfin'):
        endpoint = attempt(service_endpoint, service, runtime)
        url = runtime.urls[service]
        entry = report[service]
        entry['endpoint_ok'] = endpoint[0]
        entry['endpoint_identical_to_urls'] = endpoint[0] and endpoint[1][0] == url
        if service != 'jellyfin':
            executor = attempt(old_executor_arr, runtime.containers[service])
            entry['endpoint_identical_to_url_base_executors'] = (
                endpoint[0] and executor[0] and endpoint[1][0] == url + executor[1][1])
    report['identical'] = all(v for service in report.values() for k, v in service.items() if 'identical' in k)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    from runtime_config import get_config
    report = compare(get_config())
    print(json.dumps(report, indent=2))
    return 0 if report['identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
