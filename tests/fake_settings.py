"""An in-memory SettingsBackend/HealthBackend for testing the settings pages and the
setup wizard before the real settings layer (plan phase 1) exists. It follows
settings_contract.py exactly; its fields are a small, representative subset."""
import copy
import hashlib
import json

import settings_contract as sc

SOURCES = (('docker_config_xml', 'Read from the container (automatic)'),
           ('config_xml', 'Read from config.xml on this machine'),
           ('file', 'Paste a key (saved owner-only)'))
PROVIDERS = (('Hulu', 'Hulu'), ('Peacock', 'Peacock'), ('Netflix', 'Netflix'))


def service_fields(name, label):
    p = 'services.' + name
    return [sc.Field(p + '.url', 'URL', sc.URL, 'For example http://localhost:7878', True, group=label),
            sc.Field(p + '.source', 'API key', sc.CHOICE, choices=SOURCES, group=label),
            sc.Field(p + '.key', 'Pasted key', sc.SECRET, 'Only used with "Paste a key".', group=label)]


class FakeSettings:
    def __init__(self, configured=True):
        self.configured = configured
        self.job_running = False
        self.capabilities_ = sc.Capabilities(topologies=('nas_ssh',), secret_file='~/.config/migratarr/secrets.json')
        self.saves, self.created = [], []
        self.store = {} if not configured else {
            sc.SERVICES: {'services.radarr.url': 'http://localhost:7878', 'services.radarr.source': 'file',
                          'services.radarr.key': 'RADARR-SECRET-1234',
                          'services.sonarr.url': 'http://localhost:8989', 'services.sonarr.source': 'docker_config_xml',
                          'services.sonarr.key': None},
            sc.TOPOLOGY: {'topology.mode': 'nas_ssh', 'topology.nas.host': 'nas.example'},
            sc.DISKS: {'disks.media01.path': '/mnt/nas/media01', 'disks.media01.min_free_gb': 50},
            sc.STREAMING: {'streaming.region': 'US', 'streaming.subscribed': ['Hulu']},
            sc.SCORING: {'scoring.weights.replacement': 0.45, 'scoring.weights.streaming': 0.30,
                         'scoring.weights.usage': 0.25},
            sc.OVERRIDES: {'overrides.lock_tag': 'migratarr-lock'},
        }
        self.problems = []
        self.checks = [sc.CheckResult('radarr', 'Radarr reachable', sc.OK, 'Radarr 5.x'),
                       sc.CheckResult('nas', 'NAS over SSH', sc.WARN, 'Python 3.8 found; 3.10 recommended')]
        self.draft_checks = None

    # contract
    def status(self):
        rev = hashlib.sha256(json.dumps(self.store, sort_keys=True, default=str).encode()).hexdigest()[:12]
        return sc.Status(self.configured, rev, self.job_running, tuple(self.problems))

    def capabilities(self):
        return self.capabilities_

    def sections(self):
        return [sc.SectionSpec(sc.SERVICES, 'Services', 'Radarr, Sonarr and their keys'),
                sc.SectionSpec(sc.TOPOLOGY, 'Where moves run', 'NAS over SSH or local disks'),
                sc.SectionSpec(sc.DISKS, 'Disks and categories', 'Disks, folders and free-space reserves'),
                sc.SectionSpec(sc.STREAMING, 'Streaming', 'Region and subscriptions'),
                sc.SectionSpec(sc.SCORING, 'Scoring', 'Weights'),
                sc.SectionSpec(sc.OVERRIDES, 'Override tags', 'Arr tags that lock or pin items')]

    def fields(self, section, values):
        if section == sc.SERVICES:
            return service_fields('radarr', 'Radarr') + service_fields('sonarr', 'Sonarr')
        if section == sc.TOPOLOGY:
            return [sc.Field('topology.mode', 'Moves run', sc.CHOICE, required=True,
                             choices=(('nas_ssh', 'On a NAS over SSH'), ('local', 'On this machine\'s disks'))),
                    sc.Field('topology.nas.host', 'NAS host', sc.TEXT, 'Only for NAS over SSH.')]
        if section == sc.DISKS:
            ids = sorted({p.split('.')[1] for p in values if p.startswith('disks.')}) or ['media01']
            out = []
            for d in ids:
                out += [sc.Field(f'disks.{d}.path', 'Mounted at', sc.PATH, required=True, group='Disk ' + d),
                        sc.Field(f'disks.{d}.min_free_gb', 'Keep free (GB)', sc.INTEGER, group='Disk ' + d)]
            return out
        if section == sc.STREAMING:
            return [sc.Field('streaming.region', 'Region', sc.TEXT, 'Two letters, e.g. US', True),
                    sc.Field('streaming.subscribed', 'Subscribed', sc.MULTI, choices=PROVIDERS)]
        if section == sc.SCORING:
            return [sc.Field('scoring.weights.' + k, k.capitalize() + ' weight', sc.NUMBER, group='Weights')
                    for k in ('replacement', 'streaming', 'usage')]
        if section == sc.OVERRIDES:
            return [sc.Field('overrides.lock_tag', 'Lock tag', sc.TEXT, required=True)]
        raise sc.SettingsError('no section ' + section)

    def current(self, section):
        values = dict(self.store.get(section, {}))
        for f in self.fields(section, values):
            if f.kind == sc.SECRET:
                values[f.path] = sc.SECRET_KEPT if values.get(f.path) else None
        return values

    def defaults(self, section):
        return {sc.TOPOLOGY: {'topology.mode': 'nas_ssh'}, sc.STREAMING: {'streaming.region': 'US'},
                sc.SERVICES: {'services.radarr.source': 'docker_config_xml',
                              'services.sonarr.source': 'docker_config_xml'}}.get(section, {})

    def validate(self, section, values, draft=None):
        out = []
        for path, value in values.items():
            if path.endswith('.url') and value and not str(value).startswith(('http://', 'https://')):
                out.append(sc.Problem(path, 'Use an http:// or https:// address.'))
            if path.endswith('.source') and value == 'file':
                key = path[:-len('source')] + 'key'
                if values.get(key) in (None, ''):
                    out.append(sc.Problem(key, 'Paste the key, or choose another way to read it.'))
        if section == sc.STREAMING and not str(values.get('streaming.region') or '').isupper():
            out.append(sc.Problem('streaming.region', 'Use two uppercase letters.'))
        if section == sc.TOPOLOGY and values.get('topology.mode') == 'nas_ssh' and not values.get('topology.nas.host'):
            out.append(sc.Problem('topology.nas.host', 'Enter the NAS host name.'))
        if section == sc.SCORING:
            total = sum(float(v or 0) for v in values.values())
            if abs(total - 1) > 1e-9:
                out.append(sc.Problem(sc.SCORING, f'Weights add up to {total:g}, not 1.', sc.WARN))
        return out

    def diff(self, section, values):
        before = self.current(section)
        out = []
        for f in self.fields(section, values):
            old, new = before.get(f.path), values.get(f.path)
            if f.kind == sc.SECRET:
                if new not in (sc.SECRET_KEPT, old):
                    out.append(sc.Change(f.path, f.label, old, sc.SECRET_NEW if new else None, True))
            elif old != new:
                out.append(sc.Change(f.path, f.label, old, new))
        return out

    def save(self, section, values, revision):
        if self.job_running:
            raise sc.SaveRefused('A job is running. Save after it finishes.')
        if revision != self.status().revision:
            raise sc.SaveRefused('The settings files changed since you reviewed this.')
        merged = dict(self.store.get(section, {}))
        for path, value in values.items():
            if value is not sc.SECRET_KEPT:
                merged[path] = value
        self.store[section] = merged
        self.saves.append((section, copy.deepcopy(merged)))
        return sc.SaveResult(('config/' + section + '.json',), ('config/' + section + '.json.backup-1',))

    def discover(self, section, draft):
        if section == sc.SERVICES:
            return sc.Discovery({'services.radarr.url': 'http://localhost:7878',
                                 'services.sonarr.url': 'http://localhost:8989'},
                                ('Found Radarr and Sonarr on this machine; their keys can be read automatically.',))
        if section == sc.DISKS:
            return sc.Discovery({'disks.media01.path': '/mnt/nas/media01', 'disks.media02.path': '/mnt/nas/media02'},
                                ('Found 2 mounted disks used by Radarr root folders.',))
        return sc.Discovery({}, ())

    def create(self, draft):
        if self.configured:
            raise sc.SaveRefused('Already set up.')
        self.store = copy.deepcopy(draft)
        self.created.append(copy.deepcopy(draft))
        self.configured = True
        return sc.SaveResult(('config/runtime.json', 'config/storage-targets.json', 'config/planner.json'),
                             ('', '', ''))

    # HealthBackend
    def run_checks(self, draft=None):
        if draft is not None:
            self.draft_checks = draft
        return list(self.checks)
