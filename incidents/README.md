# Incident recovery scripts (frozen)

One-off scripts that recovered two specific moves on the maintainer's library
in September 2026. They are kept unchanged as evidence and are covered by
tests (`IncidentRecoveryTests`), but they are **not installed** by
`pip install migratarr` and refuse to run on any other move.

| Script | Incident |
| --- | --- |
| `recover_cross_0102.py` | `20260915T192959Z-0102`: NAS copy published, response timed out (1800 s limit) |
| `recover_cross_0116.py` | `20260915T192959Z-0116`: copy finished, move stopped before the Radarr update |

For a move that stopped after the NAS published its copy, use the general
tool instead: `recover_cross_published.py` (see
[docs/recover-published-copy.md](../docs/recover-published-copy.md)).

From a clone, these run as modules from the repository root:
`python3 -m incidents.recover_cross_0102`.
