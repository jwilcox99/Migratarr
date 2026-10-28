"""Transfer strategies (unified-execution-core D1): the ordered steps of one move.

  SameDiskRename  RENAME_INTENT -> NAS renameat2 -> RENAMED -> NFS visibility
                  check -> Arr update -> SUCCESS
  CrossDiskCopy   COPY_INTENT -> NAS staged copy + atomic publish -> COPIED ->
                  destination verify -> Arr update -> DELETE_INTENT ->
                  quarantine + verified delete -> SOURCE_REMOVED -> SUCCESS

A strategy owns the order, the intent/confirmation events and when inputs are
re-verified; the adapter (adapters.py) owns every Arr check. Both together
reproduce, step for step, what each of the four executors did before gate 3
(tests/golden/executor_sequences.json pins the journal and the full trace of
plan reloads, inventories, Arr reads and NAS calls).

Every helper is looked up in the executor's names (`ns`) at call time:
load_plan, paths, inventory, file_metadata, verify_nfs_after_rename, ...
"""
import os
from pathlib import PurePosixPath

from migratarr.engine.adapters import ADAPTERS
from migratarr.engine.nas_side import require


class Strategy:
    cross = None

    def __init__(self, ns, media, arr, log, transport):
        self.ns, self.log, self.transport = ns, log, transport
        self.adapter = ADAPTERS[media](ns, arr, self.cross)
        self.arr = arr

    def approved(self, base, execution_id, plan, message):
        require(self.ns.load_plan(base, execution_id) == plan, message)

    def unchanged_source(self, src, before):
        require(self.ns.file_metadata(src) == self.ns.inventory_metadata(before), 'Source changed during preflight')


class SameDiskRename(Strategy):
    cross = False
    # Per-pairing preflight differences, kept as they were (D8).
    OPENING = {'Movie': 'Checking frozen manifest, approval, paths, and Radarr record',
               'TV': 'Checking manifest, approval, and Sonarr series'}

    def execute(self, base, execution_id, live, recovery=None):
        ns, a, log, transport = self.ns, self.adapter, self.log, self.transport
        movie = a.media == 'Movie'
        ns.progress(self.OPENING[a.media])
        row, manifest_hash = ns.load_plan(base, execution_id)
        if movie:
            system = a.status()
        src, dst, logical_src, logical_dst = ns.paths(row)
        if not movie:
            # Validate the configured NAS mapping even for a check-only run.
            ns.remote_path(src)
            ns.remote_path(dst)
        ns.filesystem_ready(src, dst)
        if not movie:
            system = a.status()
        a.find(row, logical_src, logical_dst)
        root = str(PurePosixPath(logical_dst).parent)
        a.check_root(root)
        a.check_tags(row)
        a.before_inventory()
        before = ns.inventory(src)
        if recovery is not None:
            old, intent = recovery
            require(old['manifest_sha256'] == manifest_hash and old['source'] == str(src)
                    and old['destination'] == str(dst) and intent['inventory'] == before,
                    'Failed-attempt manifest, paths, or source contents have changed')
        a.source_contents(logical_src, before)
        self.arr.visible(root, '-d')
        require(transport is not None, 'NAS transport required')
        transport.call('check', src, dst, before)
        if recovery is not None:
            log('NFS_RECOVERY_VERIFIED', manifest_sha256=manifest_hash)
        log('PREFLIGHT_OK', manifest_sha256=manifest_hash, source=str(src), destination=str(dst),
            **a.identity(system), logical_source=logical_src, logical_destination=logical_dst,
            **a.preflight_extra())
        if not live:
            log('CHECK_ONLY', manifest_sha256=manifest_hash)
            return 'CHECK_ONLY'
        # Revalidate authorization and mutable inputs immediately before durable intent.
        self.approved(base, execution_id, (row, manifest_hash), a.text['plan'])
        a.revalidate(logical_src)
        ns.filesystem_ready(src, dst)
        self.unchanged_source(src, before)
        log('RENAME_INTENT', inventory=before, **a.intent_details())
        transport.call('rename', src, dst, before)
        log('RENAMED')
        ns.verify_nfs_after_rename(src, dst, before, log)
        a.visible_at(logical_dst)
        a.before_update(logical_src)
        log(a.update_event)
        a.update(logical_dst, root)
        a.verify_updated(logical_dst, root)
        ns.verify_nfs_after_rename(src, dst, before, log, metadata_only=True)
        a.final_check(logical_dst, root)
        log('SUCCESS', manifest_sha256=manifest_hash, **a.success_extra())
        return 'SUCCESS'


class CrossDiskCopy(Strategy):
    cross = True

    def execute(self, base, execution_id, live):
        ns, a, log, transport = self.ns, self.adapter, self.log, self.transport
        row, manifest_hash = ns.load_plan(base, execution_id)
        src, dst, logical_src, logical_dst = ns.paths(row)
        ns.canonical_existing(src)
        ns.canonical_existing(dst.parent)
        require(src.is_dir() and dst.parent.is_dir() and not os.path.lexists(dst), 'Source/destination not ready')
        system = a.status()
        a.find(row, logical_src, logical_dst)
        root = str(PurePosixPath(logical_dst).parent)
        a.check_root(root)
        a.check_tags(row)
        before = ns.inventory(src)
        a.source_contents(logical_src, before)
        self.arr.visible(root, '-d')
        transport.call('check', src, dst, before, execution_id)
        log('PREFLIGHT_OK', manifest_sha256=manifest_hash, source=str(src), destination=str(dst),
            logical_source=logical_src, logical_destination=logical_dst, **a.identity(system),
            **a.preflight_extra())
        if not live:
            log('CHECK_ONLY', manifest_sha256=manifest_hash)
            return 'CHECK_ONLY'
        plan = (row, manifest_hash)
        self.approved(base, execution_id, plan, a.text['plan'])
        a.revalidate(logical_src)
        self.unchanged_source(src, before)
        log('COPY_INTENT', inventory=before, **a.intent_details())
        receipt = transport.call('copy', src, dst, before, execution_id)
        log('COPIED', receipt=receipt)
        destination = ns.verify_destination(dst, before)
        require(ns.inventory(src) == before, 'Source changed while copying; retain both copies')
        a.verify_contents(logical_dst)
        self.approved(base, execution_id, plan, 'Approval changed after copy')
        a.before_update(logical_src)
        log(a.update_event)
        a.update(logical_dst, root)
        a.verify_updated(logical_dst, root)
        require(ns.file_metadata(dst) == ns.inventory_metadata(destination), 'Destination changed after verification')
        self.approved(base, execution_id, plan, 'Approval changed before deletion')
        a.before_delete(logical_dst, root)
        log('DELETE_INTENT', receipt=receipt)
        transport.call('delete', src, dst, before, execution_id, receipt)
        log('SOURCE_REMOVED')
        ns.wait_source_absent(src)
        require(ns.file_metadata(dst) == ns.inventory_metadata(destination), 'Final destination metadata mismatch')
        a.final_check(logical_dst, root)
        log('SUCCESS', manifest_sha256=manifest_hash, **a.success_extra())
        return 'SUCCESS'


STRATEGIES = {'SAME_DISK_RENAME': SameDiskRename, 'CROSS_DISK_TRANSFER': CrossDiskCopy}
