# Movie executor v1 — 2026-09-16

## Validated milestone
- All 13 media04 same-disk movie moves in run 20260915T192959Z completed.
- All 13 passed a subsequent read-only audit of journals, filesystem
  metadata, source absence, and Radarr paths.
- The audit did not rehash file contents.

## Execution contract
- One explicitly supplied, approved Movie + SAME_DISK_RENAME execution ID.
- Frozen manifest checksums and hash-bound approval verification.
- Protected, non-overwriting rename performed directly on the NAS.
- Configured mapping: /mnt/nas/media04 to /volume3/media04.
- Radarr path update with moveFiles=false and post-update verification.
- Durable execution journals and refusal to repeat uncertain live attempts.
- Dedicated SSH key authentication, persistent connections, and NFS
  visibility retries.

## Latest optimization
- Five full-content verification passes per live move instead of ten.
- Metadata checks between content checks.
- Stage, hashing, and elapsed-time progress output.
- 50 local tests passed before installation; this optimized version has
  not yet performed a live move.

## Scope
TV and cross-disk transfers remain unsupported.
Credentials, approvals, manifests, snapshots, and execution journals
remain local and are not part of this release.
