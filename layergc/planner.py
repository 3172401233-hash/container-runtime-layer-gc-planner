"""Layer garbage-collection planning.

The planner is a pure function over the input model: it classifies every
known layer as ``delete``, ``retain``, ``observe`` or ``quarantine`` and
explains each decision with reference chains and blocking reasons.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .model import (
    Container,
    ImageManifest,
    LayerRecord,
    Lease,
    Mount,
    Tag,
)

DELETE = "delete"
RETAIN = "retain"
OBSERVE = "observe"
QUARANTINE = "quarantine"


@dataclass(frozen=True)
class LayerDecision:
    """The planner's verdict for a single layer."""

    digest: str
    action: str
    size: int
    reasons: Tuple[str, ...] = ()
    referenced_by: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GCPlan:
    """Result of a planning pass."""

    decisions: Tuple[LayerDecision, ...]
    freed_bytes: int
    reference_chains: Dict[str, Tuple[str, ...]]
    blocking_reasons: Dict[str, Tuple[str, ...]]
    issues: Tuple[str, ...] = ()

    def by_action(self, action: str) -> Tuple[LayerDecision, ...]:
        return tuple(d for d in self.decisions if d.action == action)

    @property
    def deletable(self) -> Tuple[LayerDecision, ...]:
        return self.by_action(DELETE)

    @property
    def retained(self) -> Tuple[LayerDecision, ...]:
        return self.by_action(RETAIN)

    @property
    def observing(self) -> Tuple[LayerDecision, ...]:
        return self.by_action(OBSERVE)

    @property
    def quarantined(self) -> Tuple[LayerDecision, ...]:
        return self.by_action(QUARANTINE)


class LayerGCPlanner:
    """Computes a GC plan from manifests, tags, containers, leases, mounts."""

    def __init__(
        self,
        layers: Sequence[LayerRecord],
        manifests: Sequence[ImageManifest] = (),
        tags: Sequence[Tag] = (),
        containers: Sequence[Container] = (),
        leases: Sequence[Lease] = (),
        mounts: Sequence[Mount] = (),
    ) -> None:
        self._layers: Dict[str, LayerRecord] = {}
        for layer in layers:
            if layer.digest in self._layers:
                raise ValueError("duplicate layer digest: %s" % layer.digest)
            self._layers[layer.digest] = layer
        self._manifests = {m.manifest_id: m for m in manifests}
        self._tags = list(tags)
        self._containers = list(containers)
        self._leases = list(leases)
        self._mounts = list(mounts)

    def plan(self, now: Optional[float] = None) -> GCPlan:
        referenced_by: Dict[str, List[str]] = {
            digest: [] for digest in self._layers
        }
        issues: List[str] = []

        def pin(digest: str, chain: str) -> None:
            if digest in referenced_by:
                referenced_by[digest].append(chain)
            else:
                issues.append(
                    "unknown layer digest %s referenced by %s" % (digest, chain)
                )

        def pin_manifest(manifest_id: str, chain: str) -> None:
            manifest = self._manifests.get(manifest_id)
            if manifest is None:
                issues.append(
                    "unknown manifest %s referenced by %s" % (manifest_id, chain)
                )
                return
            for digest in manifest.layers:
                pin(digest, "%s -> manifest %s" % (chain, manifest_id))

        for tag in self._tags:
            pin_manifest(tag.manifest_id, "tag %s" % tag.name)

        for container in self._containers:
            if not container.running:
                continue
            chain = "running container %s" % container.container_id
            pin_manifest(container.manifest_id, chain)
            for digest in container.layers:
                pin(digest, chain)

        for lease in self._leases:
            if lease.expires_at is not None and now is not None:
                if lease.expires_at <= now:
                    continue
            for digest in lease.digests:
                pin(digest, "lease %s" % lease.lease_id)

        for mount in self._mounts:
            for digest in mount.digests:
                pin(digest, "mount %s" % mount.mount_id)

        # Chain closure: a referenced layer also pins all of its ancestors,
        # so a parent is never collected from under a live child.
        for digest in list(referenced_by):
            if not referenced_by[digest]:
                continue
            seen = set()
            parent = self._layers[digest].parent
            while parent is not None and parent not in seen:
                seen.add(parent)
                record = self._layers.get(parent)
                if record is None:
                    issues.append(
                        "layer %s has unknown parent %s" % (digest, parent)
                    )
                    break
                referenced_by[parent].append("ancestor of %s" % digest)
                parent = record.parent

        # First pass: referenced layers are retained, corrupt layers are
        # quarantined (isolated, never reused, never auto-deleted).
        actions: Dict[str, str] = {}
        reasons: Dict[str, List[str]] = {d: [] for d in self._layers}
        for digest, record in self._layers.items():
            if record.corrupt:
                actions[digest] = QUARANTINE
                reasons[digest].append(
                    "corrupt digest: quarantined, not reused or deleted"
                )
            elif referenced_by[digest]:
                actions[digest] = RETAIN
            else:
                actions[digest] = DELETE

        # Second pass: an unreferenced layer may only be deleted together
        # with its parent chain. If the parent is kept (retained, observed
        # or quarantined), deleting just the child record would corrupt the
        # chain bookkeeping, so the child is deferred to observation.
        changed = True
        while changed:
            changed = False
            for digest, record in self._layers.items():
                if actions[digest] != DELETE:
                    continue
                parent = record.parent
                if parent is None:
                    continue
                if parent not in actions or actions[parent] == DELETE:
                    continue
                actions[digest] = OBSERVE
                reasons[digest].append(
                    "parent %s is kept; cannot remove only the child record"
                    % parent
                )
                changed = True

        decisions: List[LayerDecision] = []
        freed = 0
        reference_chains: Dict[str, Tuple[str, ...]] = {}
        blocking: Dict[str, Tuple[str, ...]] = {}
        for digest in sorted(self._layers):
            record = self._layers[digest]
            action = actions[digest]
            chains = tuple(referenced_by[digest])
            reference_chains[digest] = chains
            layer_reasons = list(reasons[digest])
            if action == RETAIN:
                layer_reasons.extend(chains)
            if action != DELETE:
                blocking[digest] = tuple(layer_reasons)
            else:
                freed += record.size
            decisions.append(
                LayerDecision(
                    digest=digest,
                    action=action,
                    size=record.size,
                    reasons=tuple(layer_reasons),
                    referenced_by=chains,
                )
            )

        return GCPlan(
            decisions=tuple(decisions),
            freed_bytes=freed,
            reference_chains=reference_chains,
            blocking_reasons=blocking,
            issues=tuple(issues),
        )
