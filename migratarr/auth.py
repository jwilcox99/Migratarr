#!/usr/bin/env python3
"""Sign-in for the web UI: one account, a hashed password, signed session cookies.

    migratarr password              # set or change the UI login (asks for both)

The account lives in the data folder as config/auth.json, readable by its owner
only. The password is stored as a PBKDF2-SHA256 hash, never as text. A session is
a signed cookie that lasts SESSION_DAYS; changing the password ends every session.

When the UI asks for a login (migratarr/ui.py):
- whenever an account exists;
- always when the UI listens beyond this machine (--host other than 127.0.0.1),
  where the first visit creates the account.
Standard library only; never prints a password, hash or secret.
"""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import time

ITERATIONS = 600_000
SESSION_DAYS = 30
MIN_PASSWORD = 8
MAX_USERNAME = 64
FILE_NAME = 'auth.json'


class AuthError(ValueError):
    """A problem with the account file or what someone typed. The message is safe to show."""


def default_path(environ=None):
    from migratarr.paths import config_dir
    return config_dir(environ) / FILE_NAME


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    return f'pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}'


def password_matches(password, stored):
    try:
        scheme, iterations, salt, digest = stored.split('$')
        if scheme != 'pbkdf2_sha256':
            return False
        expected = hashlib.pbkdf2_hmac('sha256', password.encode(), _unb64(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, _unb64(digest))


def check_new_login(username, password, confirm=None):
    """Reasons a new login is refused, in words for a form ('' when fine)."""
    username = (username or '').strip()
    if not username:
        return 'Enter a username.'
    if len(username) > MAX_USERNAME or any(ord(c) < 32 or ord(c) == 127 for c in username):
        return f'Use a username of up to {MAX_USERNAME} characters, without control characters.'
    if len(password or '') < MIN_PASSWORD:
        return f'Use a password of at least {MIN_PASSWORD} characters.'
    if confirm is not None and password != confirm:
        return "The two passwords don't match."
    return ''


class Accounts:
    """The one UI account in ``path`` (config/auth.json)."""

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else default_path()
        self.clock = clock

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise AuthError(f'Cannot read the UI login file {self.path}: {exc}') from None
        if not (isinstance(data, dict) and all(isinstance(data.get(k), str) for k in ('username', 'password',
                                                                                         'secret'))):
            raise AuthError(f'The UI login file {self.path} is damaged; set the login again with '
                            '"migratarr password".')
        return data

    def exists(self):
        return self._read() is not None

    def username(self):
        data = self._read()
        return data['username'] if data else None

    def set(self, username, password, replace=False):
        """Create the account (or, with ``replace``, change it). A new secret ends every session."""
        problem = check_new_login(username, password)
        if problem:
            raise AuthError(problem)
        if self.exists() and not replace:
            raise AuthError('A login already exists. Change it with "migratarr password".')
        data = dict(version=1, username=username.strip(), password=hash_password(password),
                    secret=secrets.token_urlsafe(32), changed_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name('.' + self.path.name + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def verify(self, username, password):
        data = self._read()
        if not data:
            return False
        same_name = hmac.compare_digest((username or '').strip().encode(), data['username'].encode())
        return password_matches(password or '', data['password']) and same_name

    # ---- sessions: "v1.<username>.<expires>.<signature>", signed with the secret and the password hash

    def _sign(self, data, body):
        key = (data['secret'] + data['password']).encode()
        return _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())

    def issue(self):
        data = self._read()
        if not data:
            raise AuthError('No login is set.')
        body = 'v1.' + _b64(data['username'].encode()) + '.' + str(int(self.clock()) + SESSION_DAYS * 86400)
        return body + '.' + self._sign(data, body)

    def session_user(self, cookie):
        """The signed-in username for a session cookie value, or None."""
        try:
            data = self._read()
        except AuthError:
            return None
        if not data or not cookie or cookie.count('.') != 3:
            return None
        body, signature = cookie.rsplit('.', 1)
        if not hmac.compare_digest(signature.encode(), self._sign(data, body).encode()):
            return None
        _, name, expires = body.split('.')
        try:
            if int(expires) < self.clock():
                return None
            name = _unb64(name).decode()
        except ValueError:
            return None
        return name if name == data['username'] else None

    def form_token(self, cookie):
        """A per-session token for the sign-out form."""
        data = self._read()
        return self._sign(data, 'form.' + (cookie or '')) if data else ''


class Throttle:
    """Slows down password guessing: after LIMIT failures in WINDOW seconds, an address waits."""
    LIMIT, WINDOW = 5, 300

    def __init__(self, clock=time.monotonic):
        self.clock, self.failures = clock, {}

    def _recent(self, address):
        now = self.clock()
        kept = [t for t in self.failures.get(address, []) if now - t < self.WINDOW]
        self.failures[address] = kept
        return kept

    def blocked_for(self, address):
        """Seconds to wait (0 when a try is allowed)."""
        recent = self._recent(address)
        if len(recent) < self.LIMIT:
            return 0
        return max(1, int(self.WINDOW - (self.clock() - recent[-self.LIMIT])))

    def failed(self, address):
        self._recent(address).append(self.clock())

    def succeeded(self, address):
        self.failures.pop(address, None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='migratarr password', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--username', help='username (asked when left out)')
    args = parser.parse_args(argv)
    accounts = Accounts()
    try:
        current = accounts.username()
    except AuthError as exc:
        print(exc, file=sys.stderr)
        current = None
    if not sys.stdin.isatty():
        print('Run this in a terminal: it asks for the password and never takes it as an argument.',
              file=sys.stderr)
        return 2
    username = args.username or input(f'Username [{current}]: ' if current else 'Username: ').strip() or current
    password = getpass.getpass('New password: ')
    confirm = getpass.getpass('Same password again: ')
    problem = check_new_login(username, password, confirm)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    try:
        accounts.set(username, password, replace=True)
    except (AuthError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f'UI login {"changed" if current else "set"} for {username.strip()} in {accounts.path}. '
          'Anyone signed in has to sign in again. Restarting the UI is not needed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
