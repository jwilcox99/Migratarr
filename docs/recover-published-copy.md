# Recovering a published copy after a NAS timeout

`recover_cross_published.py` finishes a cross-disk Movie or TV move that stopped
in one specific state: the NAS **published** the copy, but its response didn't
come back before the executor's time limit.

This happened twice:
- *Moneyball*, `20260915T192959Z-0102`, at the old 1800 s limit. It was fixed with the one-off `recover_cross_0102.py`.
- *Friday Night Lights*, `20260923T013125Z-0014`, 160 GB, at the 7200 s limit.

Since 2026-09-27 the executors stop a NAS call only after 30 minutes with **no progress** (`NAS_IDLE_TIMEOUT`), rather than after a fixed total time. This failure should now be rare, and this tool handles it when it does happen.

## When it applies

The tool refuses unless every one of these holds:

- The move's journal ends with `START (live) → PREFLIGHT_OK → COPY_INTENT → STOPPED (TimeoutExpired)`, and has no later mutation or recovery event.
- Both the source and the published destination folders exist, and no `.migratarr-stage-<id>` or `.migratarr-delete-<id>` folder is left behind.
- The source still hash-matches the inventory recorded in `COPY_INTENT`, and so does the destination. Both are checked over NFS and again on the NAS.
- Radarr/Sonarr still point at the source with the same file(s):
  - Movie: the same movie file.
  - TV: the same episode files and episode ↔ file associations.
- The item isn't locked, and no override tag conflicts with the move.
- The approval is still current for the manifest hash.

## Use

On the media host, in `tmux`, because hashing a large series takes hours:

```sh
cd /opt/media-stack/migratarr
python3 recover_cross_published.py <execution_id>            # check-only: re-verifies, writes nothing
python3 recover_cross_published.py <execution_id> --execute  # finish the move
python3 migratarr_status.py --execution <execution_id>
```

With `--execute`, the tool:

1. Journals `RECOVERY_STARTED`.
2. Updates the Arr path and verifies the Arr's view of every file.
3. Re-checks the approval.
4. Deletes the source through the NAS's verified-delete path.
5. Ends with `SUCCESS` (`recovery: true`).

`migratarr_status.py` then shows the move as `SUCCEEDED` (recovered). A refusal at any point before step 1 leaves the journal untouched. A refusal after step 1 is journaled as `STOPPED` and keeps the source.
