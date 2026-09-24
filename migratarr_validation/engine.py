"""Port of build_move_plan.py decisions with injected policy and filesystem reads.

The planner's branch order, status strings, blocker names, warning names, and
capacity reservation rules are intentionally retained. See docs/validation-engine.md
for provenance and known legacy edge cases.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .rules import RulePolicy


GIB = 1024 ** 3
@dataclass(frozen=True)
class ValidationPolicy:
    """Explicit replacements for planner globals; supply real NAS roots in callers."""

    destination_roots: Mapping[str, Mapping[str, tuple[Path, ...]]]
    source_roots: tuple[Path, ...]
    min_free_after_bytes: int = 50 * GIB
    category_paths: Mapping[str, Mapping[str, Path]] | None = None
    rules: RulePolicy = field(default_factory=RulePolicy)


@dataclass(frozen=True)
class MoveRequest:
    media_type: str
    item_id: int | None
    title: str
    source_path: str
    current: str
    recommended: str
    final_score: str = ""
    replacement: str = ""
    confidence: str = ""
    decision_reason: str = ""


def _gb(value: int | None) -> float | str:
    return "" if value is None else round(value / GIB, 2)


def apply_override(
    media_type: str,
    item_id: int | None,
    current: str,
    recommended: str,
    overrides: Mapping[str, Mapping[int | None, set[str]]],
    rules: RulePolicy | None = None,
) -> tuple[str, str, str]:
    """Ported from build_move_plan.py:471-500, including lock precedence."""
    rules = rules or RulePolicy()
    tags = overrides.get(media_type, {}).get(item_id, set())
    if not tags:
        return recommended, "", ""
    if rules.lock_tag in tags:
        return current, "LOCK", rules.lock_tag
    category_tags = [tag for tag in tags if tag in rules.category_override_tags]
    if len(category_tags) > 1:
        return recommended, "CONFLICT", ",".join(sorted(category_tags))
    if len(category_tags) == 1:
        tag = category_tags[0]
        return rules.category_override_tags[tag], "CATEGORY", tag
    return recommended, "", ""


@dataclass
class ValidationEngine:
    policy: ValidationPolicy
    exists: Callable[[Path], bool]
    size_bytes: Callable[[Path], int | None]
    free_bytes: Callable[[Path], int | None]
    overrides: Mapping[str, Mapping[int | None, set[str]]] = field(default_factory=dict)
    projected_free: dict[str, int] = field(default_factory=dict)

    def disk(self, path: Path) -> str:
        """Port the planner's NAS disk identity to configured source roots."""
        for root in self.policy.source_roots:
            if path == root or root in path.parents:
                return root.name
        return ""

    def source_category_known(self, request: MoveRequest) -> bool:
        """False for dry-run "Unknown" (or any unconfigured) source categories.

        Without category_paths the engine keeps the pre-storage-targets
        Movies/<current> search, which reports such rows as SOURCE_MISSING.
        """
        return (self.policy.category_paths is None
                or request.current in self.policy.category_paths[request.media_type])

    def resolve_source(self, request: MoveRequest) -> Path:
        """Ported candidate search and ambiguous/missing sentinels."""
        arr = Path(request.source_path)
        if self.policy.category_paths is None:
            # Legacy policies (config/legacy-storage.json) predate category_paths and
            # describe only this deployment's Movies/<category>, TV/<category> folders;
            # policies with category_paths follow storage-targets.json (media_layout.py).
            category = Path("Movies" if request.media_type == "Movie" else "TV") / request.current
        else:
            category = self.policy.category_paths[request.media_type][request.current]
        candidates = [
            root / category / arr.name
            for root in self.policy.source_roots
        ]
        found = [path for path in candidates if self.exists(path)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return Path(
                f"/__MIGRATARR_AMBIGUOUS__/{request.media_type}/"
                f"{request.current}/{arr.name}"
            )
        return arr

    def projected_free_for(self, root: Path) -> int | None:
        disk = self.disk(root)
        if disk not in self.projected_free:
            actual = self.free_bytes(root)
            if actual is not None:
                self.projected_free[disk] = actual
        return self.projected_free.get(disk)

    def choose_destination_root(
        self, media_type: str, recommended: str, source: Path
    ) -> Path | None:
        roots = [
            root
            for root in self.policy.destination_roots[media_type][recommended]
            if self.exists(root)
        ]
        if not roots:
            return None
        source_disk = self.disk(source)
        for root in roots:
            if self.disk(root) == source_disk:
                return root
        valid = [
            (free, root)
            for root in roots
            if (free := self.projected_free_for(root)) is not None
        ]
        if not valid:
            return roots[0]
        valid.sort(key=lambda item: item[0], reverse=True)
        return valid[0][1]

    def evaluate(self, request: MoveRequest) -> dict[str, object]:
        """Return planner-compatible fields for one proposed move.

        New guard: a missing destination yields both destination blockers
        instead of the original script's AttributeError on None.exists().
        """
        original_recommended = request.recommended
        recommended, override_type, override_tag = apply_override(
            request.media_type,
            request.item_id,
            request.current,
            request.recommended,
            self.overrides,
            self.policy.rules,
        )
        blockers: list[str] = []
        warnings: list[str] = []
        if override_type == "CONFLICT":
            self.policy.rules.emit("CONFLICTING_MANUAL_OVERRIDES", blockers, warnings)
        if override_type == "LOCK":
            self.policy.rules.emit("MANUAL_LOCK", blockers, warnings)
        if override_type == "CATEGORY":
            self.policy.rules.emit("MANUAL_CATEGORY_OVERRIDE", blockers, warnings)
        if not self.source_category_known(request):
            # Ported from build_move_plan.py unknown_source_category_plan():
            # no category folder to search, so no disk is probed.
            blockers.append("SOURCE_CATEGORY_UNKNOWN")
            return {
                "media_type": request.media_type,
                "title": request.title,
                "current": request.current,
                "scored_recommendation": original_recommended,
                "recommended": recommended,
                "override_type": override_type,
                "override_tag": override_tag,
                "source_path": request.source_path,
                "target_path": "",
                "size_gb": "",
                "destination_free_gb": "",
                "free_after_move_gb": "",
                "source_disk": "",
                "target_disk": "",
                "transfer_type": "",
                "final_score": request.final_score,
                "replacement": request.replacement,
                "replacement_confidence": request.confidence,
                "decision_reason": request.decision_reason,
                "arr_path_update_required": "YES" if request.current != recommended else "",
                "status": "BLOCKED",
                "blockers": ";".join(blockers),
                "warnings": ";".join(warnings),
            }
        source = self.resolve_source(request)
        size = self.size_bytes(source) if self.exists(source) else None
        dest_root = self.choose_destination_root(request.media_type, recommended, source)
        if dest_root is None:
            blockers.append("NO_ELIGIBLE_DESTINATION")
            target = Path(
                f"/__MIGRATARR_NO_DESTINATION__/"
                f"{request.media_type}/{recommended}/{source.name}"
            )
            free = None
        else:
            target = dest_root / source.name
            free = self.projected_free_for(dest_root)
        if not self.exists(source):
            blockers.append("SOURCE_MISSING")
        if dest_root is None or not self.exists(dest_root):
            blockers.append("DESTINATION_ROOT_MISSING")
        if self.exists(target) and target.resolve() != source.resolve():
            blockers.append("DESTINATION_COLLISION")
        if request.confidence in {"low", ""}:
            self.policy.rules.emit("LOW_OR_UNKNOWN_REPLACEMENT_CONFIDENCE", blockers, warnings)
        if recommended == self.policy.rules.rare_category:
            self.policy.rules.emit("RARE_PROMOTION_REVIEW", blockers, warnings)
        if recommended == self.policy.rules.archive_category:
            self.policy.rules.emit("ARCHIVE_MOVE_REVIEW", blockers, warnings)
        if (request.current == self.policy.rules.rare_category
                and recommended != self.policy.rules.rare_category):
            self.policy.rules.emit("RARE_DEMOTION_REVIEW", blockers, warnings)
        if size is not None and free is not None:
            if free - size < self.policy.min_free_after_bytes:
                blockers.append("INSUFFICIENT_DESTINATION_SPACE")
        source_disk = self.disk(source)
        target_disk = self.disk(target)
        transfer_type = (
            "SAME_DISK_RENAME"
            if source_disk and target_disk and source_disk == target_disk
            else "CROSS_DISK_TRANSFER"
        )
        if (
            not blockers
            and size is not None
            and source_disk
            and target_disk
            and source_disk != target_disk
        ):
            source_root = next(root for root in self.policy.source_roots if root.name == source_disk)
            if source_disk not in self.projected_free:
                src_free = self.free_bytes(source_root)
                if src_free is not None:
                    self.projected_free[source_disk] = src_free
            if target_disk not in self.projected_free and dest_root is not None:
                dst_free = self.free_bytes(dest_root)
                if dst_free is not None:
                    self.projected_free[target_disk] = dst_free
            projected_target = self.projected_free.get(target_disk, 0) - size
            if projected_target < self.policy.min_free_after_bytes:
                blockers.append("INSUFFICIENT_PROJECTED_SPACE")
            else:
                # The original script assumes both sampled disks are present.
                self.projected_free[source_disk] += size
                self.projected_free[target_disk] -= size
        return {
            "media_type": request.media_type,
            "title": request.title,
            "current": request.current,
            "scored_recommendation": original_recommended,
            "recommended": recommended,
            "override_type": override_type,
            "override_tag": override_tag,
            "source_path": str(source),
            "target_path": str(target),
            "size_gb": _gb(size),
            "destination_free_gb": _gb(free),
            "free_after_move_gb": _gb(free - size) if free is not None and size is not None else "",
            "source_disk": source_disk,
            "target_disk": target_disk,
            "transfer_type": transfer_type,
            "final_score": request.final_score,
            "replacement": request.replacement,
            "replacement_confidence": request.confidence,
            "decision_reason": request.decision_reason,
            "arr_path_update_required": "YES" if request.current != recommended else "",
            "status": "BLOCKED" if blockers else "READY_FOR_REVIEW",
            "blockers": ";".join(blockers),
            "warnings": ";".join(warnings),
        }

    def evaluate_candidates(self, requests: list[MoveRequest]) -> list[dict[str, object]]:
        """Port the planner's pre-override row filters and post-override skip."""
        plans = []
        for request in requests:
            if request.recommended == "HOLD" or request.current == request.recommended:
                continue
            if request.recommended not in self.policy.destination_roots[request.media_type]:
                continue
            plan = self.evaluate(request)
            if plan["current"] != plan["recommended"]:
                plans.append(plan)
        return plans

    def apply_cumulative_capacity(self, plans: list[dict[str, object]]) -> set[str]:
        """Port the planner's final disk check, including rounded GiB sizes.

        Mutates the supplied plan rows exactly as the CSV rewrite does. The
        caller must provide readable free-space samples for every source root.
        """
        roots = {root.name: root for root in self.policy.source_roots}
        disk_free = {disk: self.free_bytes(root) for disk, root in roots.items()}
        incoming = {disk: 0.0 for disk in roots}
        outgoing = {disk: 0.0 for disk in roots}
        for plan in plans:
            if plan["status"] == "BLOCKED":
                continue
            try:
                size = float(plan["size_gb"]) * GIB
            except (TypeError, ValueError):
                continue
            src, dst = plan["source_disk"], plan["target_disk"]
            if src and dst and src != dst:
                outgoing[src] += size
                incoming[dst] += size
        blocked = {
            disk for disk in roots
            if disk_free[disk] + outgoing[disk] - incoming[disk]
            < self.policy.min_free_after_bytes
        }
        for plan in plans:
            if plan["target_disk"] in blocked and plan["source_disk"] != plan["target_disk"]:
                plan["status"] = "BLOCKED"
                existing = plan["blockers"]
                plan["blockers"] = (
                    existing + ";CUMULATIVE_DESTINATION_SPACE"
                    if existing else "CUMULATIVE_DESTINATION_SPACE"
                )
        return blocked
