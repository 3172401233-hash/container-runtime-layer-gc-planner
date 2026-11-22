"""Input model for the layer GC planner."""

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class LayerRecord:
    """A content-addressed layer known to the local store.

    ``parent`` is the digest of the previous layer in its chain, or ``None``
    for a base layer. ``corrupt`` marks layers whose verified digest does not
    match the recorded digest; corrupt layers are quarantined, never reused
    and never deleted by the planner.
    """

    digest: str
    size: int
    parent: Optional[str] = None
    corrupt: bool = False


@dataclass(frozen=True)
class ImageManifest:
    """An image manifest: an ordered list of layer digests, base first."""

    manifest_id: str
    layers: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Tag:
    """A human-readable tag pointing at a manifest."""

    name: str
    manifest_id: str


@dataclass(frozen=True)
class Container:
    """A container record.

    ``running`` containers pin every layer of their image plus any of their
    own snapshot layers. Stopped containers do not pin layers by themselves.
    """

    container_id: str
    manifest_id: str
    running: bool = False
    layers: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Lease:
    """A build-time lease pinning layer digests.

    ``expires_at`` is a UNIX timestamp; a lease is active while
    ``expires_at`` is ``None`` or greater than the planner's ``now``.
    """

    lease_id: str
    digests: Tuple[str, ...] = field(default_factory=tuple)
    expires_at: Optional[float] = None


@dataclass(frozen=True)
class Mount:
    """A temporary mount pinning layer digests (e.g. an active build)."""

    mount_id: str
    digests: Tuple[str, ...] = field(default_factory=tuple)
