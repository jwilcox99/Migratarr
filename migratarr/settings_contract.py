"""The interface between the settings layer (plan phases 1-2) and the pages that edit it (phases 4-5).

Phase 1 implements ``SettingsBackend`` (loads every config file, validates per
field, saves atomically with a backup); phase 2 implements ``HealthBackend``.
The settings pages (settings_ui.py) and the first-run wizard (setup_wizard.py)
use only what is declared here, so either side can change its internals
without touching the other. See docs/dev/settings-ui-contract.md.

Rules every implementation keeps:
- A secret's value never leaves the backend: ``current`` returns SECRET_KEPT
  for a stored secret and None for none; diffs say "changed", never the value.
- Values are already typed (int, float, bool, str, list of str). The pages
  turn form text into these types; the backend validates meaning.
- ``validate`` and ``diff`` never write. ``save`` and ``create`` re-validate,
  refuse while a job runs or when ``revision`` no longer matches, back up
  every file they replace, and write atomically.
Standard library only.
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol

SECRET_KEPT = object()  # "a secret is stored (unchanged)"; compares only by identity
SECRET_NEW = object()   # in a Change: "a new secret replaces it"

# Field kinds the pages know how to draw and parse.
TEXT, URL, PATH, INTEGER, NUMBER, CHOICE, MULTI, BOOL, LINES, SECRET = (
    'text', 'url', 'path', 'integer', 'number', 'choice', 'multi', 'bool', 'lines', 'secret')
KINDS = {TEXT, URL, PATH, INTEGER, NUMBER, CHOICE, MULTI, BOOL, LINES, SECRET}

# Section ids. Topology is chosen in the wizard; the settings pages show it read-only.
SERVICES, TOPOLOGY, DISKS, STREAMING, SCORING, OVERRIDES = (
    'services', 'topology', 'disks', 'streaming', 'scoring', 'overrides')
SECTIONS = (SERVICES, TOPOLOGY, DISKS, STREAMING, SCORING, OVERRIDES)

OK, WARN, FAIL, SKIPPED = 'ok', 'warn', 'fail', 'skipped'


class SettingsError(Exception):
    """Base for refusals the pages show to the user as-is (no secret values in messages)."""


class SaveRefused(SettingsError):
    """Nothing was written: a job is running, the files changed since review, or validation failed."""


@dataclass(frozen=True)
class Field:
    path: str                 # dotted, e.g. "services.radarr.url" or "disks.media01.path"
    label: str
    kind: str
    help: str = ''
    required: bool = False
    choices: tuple = ()       # (value, label) pairs for CHOICE / MULTI
    group: str = ''           # display grouping only, e.g. "Radarr" or "Disk media01"
    read_only: bool = False


@dataclass(frozen=True)
class SectionSpec:
    id: str
    title: str
    summary: str = ''


@dataclass(frozen=True)
class Problem:
    path: str                 # a Field.path, or a section id for problems spanning fields
    message: str
    severity: str = FAIL      # FAIL blocks saving; WARN is shown but allowed


@dataclass(frozen=True)
class Change:
    path: str
    label: str
    before: object            # secrets: SECRET_KEPT or None; never a secret value
    after: object             # secrets: SECRET_NEW, SECRET_KEPT or None
    secret: bool = False


@dataclass(frozen=True)
class SaveResult:
    files: tuple              # files written
    backups: tuple            # their backups, same order ('' when a file is new)


@dataclass(frozen=True)
class Status:
    configured: bool          # False -> the UI starts in setup mode
    revision: str             # changes whenever any settings file changes on disk
    job_running: bool
    problems: tuple = ()      # Problems in the files as they are now


@dataclass(frozen=True)
class Discovery:
    values: dict              # suggested values by Field.path (never overwrite what the user typed)
    notes: tuple = ()         # plain sentences: what was found and where


@dataclass(frozen=True)
class CheckResult:
    id: str
    label: str
    status: str               # OK / WARN / FAIL / SKIPPED
    detail: str = ''
    paths: tuple = ()         # Field.paths this check is about, so pages can link to them
    fix: str = ''             # what to do about a WARN or FAIL


@dataclass
class Capabilities:
    topologies: tuple = ('nas_ssh',)   # 'local' appears once the local transport (phase 3) ships
    experimental: tuple = ()           # topologies offered with an "experimental" label
    secret_file: Optional[str] = None  # where pasted keys go (owner-only), shown to the user
    extras: dict = field(default_factory=dict)


class SettingsBackend(Protocol):
    def status(self) -> Status: ...
    def capabilities(self) -> Capabilities: ...
    def sections(self) -> list: ...                                   # [SectionSpec]
    def fields(self, section: str, values: dict) -> list: ...         # [Field], may depend on values
    def current(self, section: str) -> dict: ...                      # {path: value}
    def defaults(self, section: str) -> dict: ...                     # starting values for setup
    def validate(self, section: str, values: dict, draft: Optional[dict] = None) -> list: ...  # [Problem]
    def diff(self, section: str, values: dict) -> list: ...           # [Change]
    def save(self, section: str, values: dict, revision: str) -> SaveResult: ...
    def discover(self, section: str, draft: dict) -> Discovery: ...
    def create(self, draft: dict) -> SaveResult: ...                  # draft: {section: {path: value}}


class HealthBackend(Protocol):
    def run_checks(self, draft: Optional[dict] = None) -> list: ...   # [CheckResult]


def blocking(problems):
    return [p for p in problems if p.severity == FAIL]
