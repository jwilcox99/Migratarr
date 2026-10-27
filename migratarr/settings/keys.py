"""Store an API key the user pasted (onboarding, settings forms).

Decision 2026-10-26: pasted keys go to an owner-only file outside git and are
read back through the existing `file` credential source (service_keys.py),
which already refuses group/world-readable files and files inside the
repository. One file per service, in a 0700 folder:

    ~/.config/migratarr/secrets/<service>        (mode 0600)

store_key() returns the runtime.json "secrets" entry to save; it never logs,
returns or echoes the value. Auto-detecting Radarr/Sonarr keys from
config.xml stays the default; this is only for keys typed in by hand.
"""
import os
from pathlib import Path
import re
import tempfile

from migratarr import services

DEFAULT_FOLDER = Path('~/.config/migratarr/secrets')


class KeyStoreError(ValueError):
    pass


def secrets_folder(environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get('MIGRATARR_SECRETS_DIR') or DEFAULT_FOLDER).expanduser()


def store_key(service, value, *, folder=None, url_base=None, environ=None):
    """Write `value` for `service` owner-only; return its runtime.json secrets entry."""
    if service not in services.SERVICES:
        raise KeyStoreError('unknown service: ' + str(service))
    if 'file' not in services.SERVICES[service].sources:
        raise KeyStoreError(service + ' does not accept a file credential')
    if not isinstance(value, str):
        raise KeyStoreError('the key must be text')
    value = value.strip()
    # Keys and tokens are single-line printable text; anything else is a paste mistake.
    if not value or len(value) > 4096 or not re.fullmatch(r'[\x21-\x7e]+', value):
        raise KeyStoreError('that does not look like a ' + services.SERVICES[service].credential_label
                            + ' (one line, no spaces); nothing was saved')
    folder = Path(folder).expanduser() if folder else secrets_folder(environ)
    from service_keys import REPOSITORY
    resolved = folder.resolve()
    if resolved == REPOSITORY or REPOSITORY in resolved.parents:
        raise KeyStoreError('keep the secrets folder outside the Migratarr repository')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    target = folder / service
    old_umask = os.umask(0o077)
    try:
        fd, tmp = tempfile.mkstemp(prefix='.' + service + '.', dir=folder)
    finally:
        os.umask(old_umask)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(value + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    entry = {'source': 'file', 'path': str(target)}
    if url_base is not None:
        if not services.SERVICES[service].url_base_from_config:
            raise KeyStoreError('url_base is only for Radarr and Sonarr')
        entry['url_base'] = url_base
    return entry


def forget_key(service, *, folder=None, environ=None):
    """Remove a stored key file (when the user switches back to auto-detect)."""
    folder = Path(folder).expanduser() if folder else secrets_folder(environ)
    try:
        (folder / service).unlink()
        return True
    except FileNotFoundError:
        return False
