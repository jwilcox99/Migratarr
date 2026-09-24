"""Where a media item's folder lives: one disk root, one category folder, one name.

Three views of the same item share the category folder from
storage-targets.json `category_paths` (e.g. "Movies/Common"):

  local   <runtime.json storage.<disk>.local_path>/<category dir>/<name>
  remote  <runtime.json storage.<disk>.remote_path>/<category dir>/<name>   (NAS)
  logical <storage-targets.json arr_root, default /media>/<category dir>/<name>  (Radarr/Sonarr)

Root depth and folder names are configuration; the shape (root + category
folder + exactly one item folder) is the safety contract every executor checks.
"""
from functools import lru_cache
import os
from pathlib import PurePosixPath


def split_media_path(path, roots, category_dirs):
    """Return (root_id, category, name) if `path` is exactly <root>/<category dir>/<name>.

    Pure and dependency-free so executors can ship its source to the NAS.
    `roots` maps IDs to absolute root paths, `category_dirs` maps categories to
    relative folder paths; both must be non-overlapping (config validation
    guarantees it), so at most one root and one category can match.
    """
    p = PurePosixPath(path)
    if '..' in p.parts:
        return None
    for root_id, root in roots.items():
        root = PurePosixPath(root)
        if root not in p.parents:
            continue
        relative = p.relative_to(root)
        if len(relative.parts) < 2:
            return None
        for category, directory in category_dirs.items():
            if relative.parent == PurePosixPath(directory):
                return root_id, category, relative.name
        return None
    return None


def canonical(raw):
    """True if `raw` is already in canonical POSIX form (as manifests must be)."""
    p = PurePosixPath(raw)
    return isinstance(raw, str) and str(p) == raw and '..' not in p.parts


class MediaLayout:
    def __init__(self, runtime, targets):
        self.local_roots = {disk: entry['local_path'] for disk, entry in runtime.storage.items()}
        self.remote_roots = {disk: entry['remote_path'] for disk, entry in runtime.storage.items()}
        self.category_dirs = {media: {category: str(directory) for category, directory in categories.items()}
                              for media, categories in targets.category_paths.items()}
        self.arr_root = str(targets.arr_root)

    def parse_local(self, path, media):
        return split_media_path(path, self.local_roots, self.category_dirs[media])

    def parse_remote(self, path, media):
        return split_media_path(path, self.remote_roots, self.category_dirs[media])

    def to_remote(self, path, media):
        disk, category, name = self.parse_local(path, media)
        return str(PurePosixPath(self.remote_roots[disk]) / self.category_dirs[media][category] / name)

    def logical(self, media, category, name):
        return str(PurePosixPath(self.arr_root) / self.category_dirs[media][category] / name)

    def arr_category(self, path, media):
        """Category of a Radarr/Sonarr path, or None if it isn't <arr_root>/<category dir>/<name>."""
        found = split_media_path(path or '/', {'arr': self.arr_root}, self.category_dirs[media])
        return found[1] if found else None


@lru_cache(maxsize=1)
def get_targets():
    """Deployed storage-targets.json, verified against runtime.json; frozen per process."""
    from runtime_config import get_config
    from storage_targets import check_runtime_consistency, load_targets
    runtime = get_config()
    targets = load_targets(os.environ.get('MIGRATARR_STORAGE_TARGETS')
                           or runtime.base_path / 'config' / 'storage-targets.json')
    check_runtime_consistency(runtime, targets)
    return targets
