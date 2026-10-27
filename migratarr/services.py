"""Service registry: one descriptor per external service Migratarr talks to.

Everything that needs to know "which services exist and how they are set up"
reads this table: credential defaults (service_keys.py), the settings report,
health checks (migratarr.health) and, later, the settings forms and the
first-run wizard. Adding an integration means adding a descriptor here (and an
adapter for what Migratarr does with it), not editing each of those places.

Standard library only; nothing here contacts a service.
"""
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class HealthProbe:
    """One authenticated GET that proves a service is reachable and the key works.

    `path` is appended to the service's API root (service_keys.service_endpoint),
    or to `base_url` for services with no configurable URL. The credential goes
    in `header` (formatted with `header_format`). `expect_field` must be present
    in the JSON response.
    """
    path: str
    header: str
    header_format: str = '{key}'
    expect_field: str | None = None
    version_field: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class ServiceDescriptor:
    id: str
    label: str
    role: str                         # movies | tv | media_server | metadata
    url_setting: str | None           # runtime.json field holding its URL, if any
    default_url: str | None           # suggested URL for a fresh install
    container_setting: str | None     # runtime.json containers.<x> its docker source uses by default
    default_source: MappingProxyType  # runtime.json "secrets" entry used when none is configured
    sources: tuple                    # credential sources this service accepts
    url_base_from_config: bool        # Radarr/Sonarr: UrlBase can come from config.xml
    probe: HealthProbe
    credential_label: str = 'API key'
    help_url: str | None = None
    notes: tuple = field(default=())


def _frozen(value):
    return MappingProxyType({k: tuple(v) if isinstance(v, list) else v for k, v in value.items()})


ALL_SOURCES = ('docker_config_xml', 'config_xml', 'docker_file', 'file', 'env')

SERVICES = MappingProxyType({d.id: d for d in (
    ServiceDescriptor(
        id='radarr', label='Radarr', role='movies', url_setting='urls.radarr',
        default_url='http://localhost:7878', container_setting='containers.radarr',
        default_source=_frozen({'source': 'docker_config_xml'}),
        sources=ALL_SOURCES, url_base_from_config=True,
        probe=HealthProbe('/api/v3/system/status', 'X-Api-Key', expect_field='version',
                          version_field='version'),
        help_url='https://wiki.servarr.com/radarr/settings#security'),
    ServiceDescriptor(
        id='sonarr', label='Sonarr', role='tv', url_setting='urls.sonarr',
        default_url='http://localhost:8989', container_setting='containers.sonarr',
        default_source=_frozen({'source': 'docker_config_xml'}),
        sources=ALL_SOURCES, url_base_from_config=True,
        probe=HealthProbe('/api/v3/system/status', 'X-Api-Key', expect_field='version',
                          version_field='version'),
        help_url='https://wiki.servarr.com/sonarr/settings#security',
        notes=('The TV executors require Sonarr v4.',)),
    ServiceDescriptor(
        id='jellyfin', label='Jellyfin', role='media_server', url_setting='urls.jellyfin',
        default_url='http://localhost:8096', container_setting='containers.homepage',
        default_source=_frozen({'source': 'docker_file',
                                'paths': ['/run/secrets/jellyfin_api_key', '/run/secrets/jellyfin_key']}),
        sources=('docker_file', 'file', 'env'), url_base_from_config=False,
        probe=HealthProbe('/System/Info', 'X-Emby-Token', expect_field='Version', version_field='Version'),
        help_url='https://jellyfin.org/docs/general/server/settings/'),
    ServiceDescriptor(
        id='tmdb', label='TMDB', role='metadata', url_setting=None, default_url=None,
        container_setting=None, default_source=_frozen({'source': 'env', 'variable': 'TMDB_TOKEN'}),
        sources=('docker_file', 'file', 'env'), url_base_from_config=False,
        probe=HealthProbe('/3/configuration', 'Authorization', 'Bearer {key}',
                          expect_field='images', base_url='https://api.themoviedb.org'),
        credential_label='API Read Access Token',
        help_url='https://www.themoviedb.org/settings/api'),
)})


def get(service_id):
    return SERVICES[service_id]


def default_sources():
    """{service: secrets entry} in the plain-dict form service_keys.parse_secrets takes."""
    return {s.id: {k: list(v) if isinstance(v, tuple) else v for k, v in s.default_source.items()}
            for s in SERVICES.values()}


def default_containers():
    """{service: runtime.json containers key} for services whose default source uses docker."""
    return {s.id: s.container_setting.split('.', 1)[1] for s in SERVICES.values()
            if s.container_setting and s.default_source['source'].startswith('docker')}
