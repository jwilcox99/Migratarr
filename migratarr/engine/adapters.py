"""Arr adapters (unified-execution-core D1): what Radarr and Sonarr each check around a move.

An adapter owns the Arr side of one move: finding the item for the source
path, the snapshot that must not change (Movie: the record and its movieFile;
TV: the record plus every episode file and episode association), content
verification through the Arr's view, the path update and the checks after it.
The strategy (strategies.py) decides when each of these runs.

Behaviour-neutral (D8): every check, refusal text and Arr call is exactly what
the executor for that pairing did before gate 3, including where same-disk and
cross-disk differ (`self.cross`). Those differences are listed in TEXT and in
the few `if self.cross` branches below; each is a separate reviewed change later.

Helpers are looked up in the executor's names (`ns`) at call time, so a
patched or reloaded executor global (inventory, OVERRIDES, episode_state, ...)
is honored as before. episode_state, verify_episode_contents and verify_movie
are bound into the executors that used them (primitives.bind), because the
recovery scripts call them there.
"""
from pathlib import PurePosixPath

from migratarr.engine.nas_side import require


# Module-level functions bound into executors (primitives.bind): names resolve there.

def episode_state(sonarr, series_id, logical_path, before):
    files = sonarr.api(f'episodefile?seriesId={series_id}')
    require(files, 'Series has no episode files')
    records = {}
    names = set()
    for f in files:
        relative = f.get('relativePath', '')
        path = PurePosixPath(relative)
        require(relative and str(path) == relative and not path.is_absolute()
                and '..' not in path.parts and relative not in names, 'Invalid or duplicate episode path')
        require(f.get('seriesId') == series_id and f['id'] not in records, 'Episode file identity mismatch')
        require(f.get('path') == logical_path + '/' + relative, 'Sonarr episode path mismatch')
        require(relative in before and len(before[relative]) == 4
                and before[relative][0] > 0 and f.get('size') == before[relative][0],
                'Episode file missing or size differs: ' + relative)
        records[f['id']] = (relative, f['size'])
        names.add(relative)
    episodes = sonarr.api(f'episode?seriesId={series_id}')
    associations = {}
    for e in episodes:
        require(e.get('seriesId') == series_id and e['id'] not in associations,
                'Episode identity mismatch')
        file_id = e.get('episodeFileId', 0)
        require(not file_id or file_id in records, 'Episode references unknown file')
        require(bool(e.get('hasFile')) == bool(file_id), 'Inconsistent episode file association')
        associations[e['id']] = (e.get('seasonNumber'), e.get('episodeNumber'), file_id,
                                  e.get('monitored'))
    require({v[2] for v in associations.values() if v[2]} == set(records),
            'Episode file is not linked to any episode')
    return records, associations


def verify_episode_contents(sonarr, logical_path, records, before):
    for index, (relative, size) in enumerate(sorted(records.values()), 1):
        progress('Sonarr file %d/%d: %s (%.2f GiB)' %
                 (index, len(records), relative, size / 2**30))
        sonarr.verify_file(logical_path + '/' + relative, before[relative][1])


def verify_movie(radarr, movie_id, logical, root, file_record):
    movie = radarr.api(f'movie/{movie_id}')
    require(movie.get('id') == movie_id and movie.get('path') == logical
            and movie.get('rootFolderPath') == root and movie.get('hasFile'), 'Radarr path verification failed')
    current = movie.get('movieFile', {})
    require(all(current.get(k) == file_record.get(k) for k in ('id', 'relativePath', 'size')),
            'Radarr movie-file identity changed')
    require(current.get('path') == logical + '/' + file_record['relativePath'], 'Radarr file path mismatch')
    return movie


# Refusal texts that differ by pairing, kept as they are (D8). Key: (media, cross).
TEXT = {
    ('Movie', False): dict(
        status='Radarr system status is invalid',
        one_match='Expected exactly one Radarr movie at source path',
        source='Radarr source changed or has no file',
        occupied='Radarr destination already assigned',
        root='Destination is not an accessible Radarr root',
        locked=lambda overrides: 'Movie now has ' + overrides.lock_tag,
        conflict='Current override conflicts',
        plan='Plan/approval changed',
        preflight='Radarr record changed during preflight'),
    ('Movie', True): dict(
        status='Missing Radarr version',
        one_match='Radarr source missing/ambiguous or destination occupied',
        source='Radarr source changed',
        root='Radarr destination root inaccessible',
        locked=lambda overrides: 'Movie is locked',
        conflict='Conflicting override',
        plan='Approval or plan changed',
        preflight='Radarr changed during preflight',
        before_update='Radarr changed before update'),
    ('TV', False): dict(
        one_match='Expected exactly one Sonarr series at source',
        occupied='Destination already assigned',
        root='Destination root unavailable',
        locked=lambda overrides: 'Series has ' + overrides.lock_tag,
        conflict='Current override conflicts',
        plan='Plan or approval changed',
        preflight='Sonarr series changed during preflight',
        before_update='Sonarr changed before path update'),
    ('TV', True): dict(
        one_match='Sonarr source missing/ambiguous or destination occupied',
        root='Sonarr destination root inaccessible',
        locked=lambda overrides: 'Series is locked',
        conflict='Conflicting override',
        plan='Approval or plan changed',
        preflight='Sonarr changed during preflight',
        before_update='Sonarr changed before update'),
}


class ArrAdapter:
    """Shared lookup steps; RadarrMovie and SonarrSeries fill in the item model."""
    media = None
    collection = None   # 'movie' or 'series'
    id_fields = ()
    update_event = None

    def __init__(self, ns, arr, cross):
        self.ns, self.arr, self.cross = ns, arr, cross
        self.text = TEXT[(self.media, cross)]
        self.item_id = self.record = self.before = None

    def get(self):
        return self.arr.api(f'{self.collection}/{self.item_id}')

    def find(self, row, logical_src, logical_dst):
        items = self.arr.api(self.collection)
        matches = [x for x in items if x.get('path') == logical_src]
        occupied = any(x.get('path') == logical_dst for x in items)
        if self.cross:
            require(len(matches) == 1 and not occupied, self.text['one_match'])
        else:
            require(len(matches) == 1, self.text['one_match'])
        self.item_id = matches[0]['id']
        self.record = self.get()
        self.check_source(logical_src)
        for field in self.id_fields:
            if row.get(field):
                require(int(row[field]) == self.item_id, self.label + ' ID mismatch')
        if not self.cross:
            require(not occupied, self.text['occupied'])

    @property
    def label(self):
        return self.arr_name

    def check_root(self, root):
        require(any(r.get('path') == root and r.get('accessible') is True for r in self.arr.api('rootfolder')),
                self.text['root'])

    def check_tags(self, row):
        overrides = self.ns.OVERRIDES
        labels = {t['id']: t['label'].lower() for t in self.arr.api('tag')}
        tags = {labels.get(t, '') for t in self.record.get('tags', [])}
        require(not overrides.locked(tags), self.text['locked'](overrides))
        require(overrides.agrees(tags, row['recommended']), self.text['conflict'])

    def identity(self, system):
        prefix = self.arr_name.lower()
        return {prefix + '_id': self.item_id, prefix + '_version': system['version']}

    def update(self, logical_dst, root):
        self.arr.api(f'{self.collection}/{self.item_id}?moveFiles=false',
                     dict(self.record, path=logical_dst, rootFolderPath=root))


class RadarrMovie(ArrAdapter):
    media, collection, arr_name = 'Movie', 'movie', 'Radarr'
    id_fields = ('radarr_id', 'item_id')
    update_event = 'RADARR_UPDATE_INTENT'

    def status(self):
        system = self.arr.api('system/status')
        require(system.get('version'), self.text['status'])
        return system

    def check_source(self, logical_src):
        if self.cross:
            require(self.record.get('path') == logical_src and self.record.get('hasFile'), self.text['source'])
        else:
            require(self.record['path'] == logical_src and self.record.get('hasFile'), self.text['source'])

    def before_inventory(self):
        if self.cross:
            return
        self.relative = self.record.get('movieFile', {}).get('relativePath', '')
        rp = PurePosixPath(self.relative)
        require(self.relative and not rp.is_absolute() and '..' not in rp.parts, 'Invalid Radarr movie file')
        self.ns.progress('Verifying source contents on the media host')

    def source_contents(self, logical_src, before):
        self.before = before
        if self.cross:
            self.file_record = dict(self.record.get('movieFile', {}))
            relative = self.relative = self.file_record.get('relativePath', '')
            require(relative in before and len(before[relative]) == 4
                    and before[relative][0] == self.file_record.get('size') and before[relative][0] > 0,
                    'Radarr media file missing or size differs')
            require(not PurePosixPath(relative).is_absolute() and '..' not in PurePosixPath(relative).parts,
                    'Invalid media path')
        else:
            relative = self.relative
            require(relative in before and len(before[relative]) == 4 and before[relative][0] > 0,
                    'Radarr media file missing')
        self.verify_contents(logical_src)

    def verify_contents(self, logical):
        self.arr.verify_file(logical + '/' + self.relative, self.before[self.relative][1])

    def preflight_extra(self):
        return dict(movie_file_id=self.file_record['id']) if self.cross else {}

    def intent_details(self):
        return {}

    def revalidate(self, logical_src):
        require(self.get() == self.record, self.text['preflight'])

    def visible_at(self, logical_dst):
        self.arr.visible(logical_dst + '/' + self.relative)

    def before_update(self, logical_src):
        if self.cross:
            require(self.get() == self.record, self.text['before_update'])

    def verify_updated(self, logical_dst, root):
        if self.cross:
            updated = self.ns.verify_movie(self.arr, self.item_id, logical_dst, root, self.file_record)
            require(all(updated.get(k) == self.record.get(k) for k in ('monitored', 'qualityProfileId', 'tags')),
                    'Radarr settings changed; source retained')
        else:
            # Same-disk Movie compares no settings fields (D8).
            after = self.get()
            require(after.get('id') == self.item_id and after.get('path') == logical_dst
                    and after.get('rootFolderPath') == root
                    and after.get('hasFile')
                    and after.get('movieFile', {}).get('id') == self.record['movieFile']['id']
                    and after.get('movieFile', {}).get('relativePath') == self.relative,
                    'Radarr post-update verification failed')
        self.verify_contents(logical_dst)

    def before_delete(self, logical_dst, root):
        self.ns.verify_movie(self.arr, self.item_id, logical_dst, root, self.file_record)

    def final_check(self, logical_dst, root):
        if self.cross:
            self.ns.verify_movie(self.arr, self.item_id, logical_dst, root, self.file_record)
            self.arr.visible(logical_dst + '/' + self.relative)

    def success_extra(self):
        return {}


class SonarrSeries(ArrAdapter):
    media, collection, arr_name = 'TV', 'series', 'Sonarr'
    id_fields = ('sonarr_id', 'item_id')
    update_event = 'SONARR_UPDATE_INTENT'
    SETTINGS = ('seriesType', 'seasonFolder', 'monitored', 'qualityProfileId', 'tags')

    def status(self):
        system = self.arr.api('system/status')
        require(str(system.get('version', '')).startswith('4.'), 'Only Sonarr v4 is supported')
        return system

    def check_source(self, logical_src):
        if self.cross:
            require(self.record.get('path') == logical_src, 'Sonarr source changed')
        else:
            require(self.record.get('id') == self.item_id and self.record.get('path') == logical_src,
                    'Sonarr source changed')

    def before_inventory(self):
        pass

    def state(self, logical):
        return self.ns.episode_state(self.arr, self.item_id, logical, self.before)

    def unchanged_state(self, logical, message):
        require(self.state(logical) == (self.records, self.associations), message)

    def source_contents(self, logical_src, before):
        self.before = before
        self.records, self.associations = self.state(logical_src)
        self.verify_contents(logical_src)

    def verify_contents(self, logical):
        self.ns.verify_episode_contents(self.arr, logical, self.records, self.before)

    def counts(self):
        return dict(episode_file_count=len(self.records),
                    linked_episode_count=sum(bool(v[2]) for v in self.associations.values()))

    preflight_extra = success_extra = counts

    def intent_details(self):
        return dict(episode_files=self.records, episode_associations=self.associations)

    def revalidate(self, logical_src):
        require(self.get() == self.record, self.text['preflight'])
        self.unchanged_state(logical_src, 'Episode files or associations changed during preflight')

    def visible_at(self, logical_dst):
        for relative, size in self.records.values():
            self.arr.visible(logical_dst + '/' + relative)

    def before_update(self, logical_src):
        # Do not overwrite a Sonarr edit made while the media was moving.
        require(self.get() == self.record, self.text['before_update'])
        self.unchanged_state(logical_src, 'Episode associations changed before path update')

    def check_path(self, item, logical_dst, root):
        return item.get('path') == logical_dst and item.get('rootFolderPath') == root

    def verify_updated(self, logical_dst, root):
        after = self.get()
        require(after.get('id') == self.item_id and self.check_path(after, logical_dst, root),
                'Sonarr path verification failed')
        for field in self.SETTINGS:
            require(after.get(field) == self.record.get(field), 'Sonarr setting changed: ' + field)
        self.unchanged_state(logical_dst, 'Episode files or associations changed after update')
        self.verify_contents(logical_dst)

    def before_delete(self, logical_dst, root):
        self.unchanged_state(logical_dst, 'Episode associations changed before deletion')

    def final_check(self, logical_dst, root):
        self.unchanged_state(logical_dst, 'Episode associations changed during final verification')
        if self.cross:
            self.verify_contents(logical_dst)
        require(self.check_path(self.get(), logical_dst, root), 'Sonarr path changed during final verification')


ADAPTERS = {'Movie': RadarrMovie, 'TV': SonarrSeries}
