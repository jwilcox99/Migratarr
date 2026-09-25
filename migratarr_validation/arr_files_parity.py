"""Compare docker and host Arr file checks on real files, read-only.

For the smallest few files Radarr and Sonarr report, runs the executors'
docker check (`docker exec <container> test -f` + `sha256sum`) and the host
check (arr_files.py, with the given host roots) and reports whether each pair
agrees. Nothing is moved, written or approved. Use it to confirm a host mode
(for example a host view of the containers' media mount) before enabling it.
"""
import argparse
import json
import subprocess
import urllib.request

from arr_files import host_path, host_sha256, host_visible


def api(api_root, key, path):
    request = urllib.request.Request(api_root + '/api/v3/' + path, headers={'X-Api-Key': key})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def arr_files(service, runtime):
    """(path, size) of every file the Arr has, from its own API."""
    from service_keys import service_endpoint
    api_root, key = service_endpoint(service, runtime)
    if service == 'radarr':
        return [(m['movieFile']['path'], m['movieFile'].get('size', 0))
                for m in api(api_root, key, 'movie') if m.get('hasFile') and m.get('movieFile', {}).get('path')]
    files = []
    for series in api(api_root, key, 'series'):
        files += [(f['path'], f.get('size', 0)) for f in api(api_root, key, f'episodefile?seriesId={series["id"]}')
                  if f.get('path')]
    return files


def docker_check(container, path):
    visible = subprocess.run(['docker', 'exec', container, 'test', '-f', path], timeout=30,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    digest = (subprocess.check_output(['docker', 'exec', container, 'sha256sum', '--', path],
                                      timeout=1800, text=True).split()[0] if visible else None)
    return visible, digest


def compare(runtime, targets, host_roots, sample):
    report = {}
    for service, root in host_roots.items():
        check = {'mode': 'host', 'host_root': root}
        container = runtime.containers.get(service)
        files = sorted(arr_files(service, runtime), key=lambda item: item[1])[:sample]
        rows = []
        for path, size in files:
            docker_visible, docker_digest = docker_check(container, path)
            mapped = host_path(check, targets.arr_root, path)
            visible = host_visible(mapped, '-f')
            rows.append({'size': size, 'mapped': mapped is not None,
                         'visible_agrees': visible == docker_visible,
                         # Nothing to hash when both agree the file isn't visible.
                         'hash_agrees': (not docker_visible and not visible)
                                        or (docker_visible and visible and host_sha256(mapped) == docker_digest)})
        report[service] = {'files': len(rows), 'all_agree': bool(rows) and all(
            r['mapped'] and r['visible_agrees'] and r['hash_agrees'] for r in rows), 'rows': rows}
    report['identical'] = all(v['all_agree'] for v in report.values())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--radarr-host-root', help='host path Radarr sees as arr_root (omit: native, paths as-is)')
    parser.add_argument('--sonarr-host-root', help='host path Sonarr sees as arr_root (omit: native, paths as-is)')
    parser.add_argument('--sample', type=int, default=3, help='smallest N files per service (default 3)')
    args = parser.parse_args(argv)
    from media_layout import get_targets
    from runtime_config import get_config
    report = compare(get_config(), get_targets(),
                     {'radarr': args.radarr_host_root, 'sonarr': args.sonarr_host_root}, args.sample)
    print(json.dumps(report, indent=2))
    return 0 if report['identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
