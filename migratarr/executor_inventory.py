"""Read-only inventory metadata primitives, also embedded in NAS helpers."""
import stat


def scan_metadata(root, require):
    """Collect names/types/size/inode/mtime; reject links and nested devices."""
    device = root.stat().st_dev
    found = {}
    def visit(folder):
        for p in sorted(folder.iterdir()):
            s = p.lstat()
            require(s.st_dev == device and not stat.S_ISLNK(s.st_mode), 'Link or nested device found')
            key = p.relative_to(root).as_posix()
            if stat.S_ISDIR(s.st_mode):
                found[key] = ['directory']
                visit(p)
            else:
                require(stat.S_ISREG(s.st_mode), 'Nonregular file found')
                found[key] = [s.st_size, s.st_ino, s.st_mtime_ns]
    visit(root)
    return found


def metadata_from_inventory(items):
    return {k: v if len(v) == 1 else [v[0], v[2], v[3]] for k, v in items.items()}
