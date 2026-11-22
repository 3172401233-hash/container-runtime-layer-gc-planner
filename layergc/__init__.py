"""Container image layer garbage-collection planner.

Pure planning library: it computes which layers may be deleted, which must
be retained, and which need further observation. It never touches the
filesystem or a real container runtime.
"""

from .model import (
    Container,
    ImageManifest,
    LayerRecord,
    Lease,
    Mount,
    Tag,
)
from .planner import GCPlan, LayerDecision, LayerGCPlanner

__all__ = [
    "Container",
    "GCPlan",
    "ImageManifest",
    "LayerDecision",
    "LayerGCPlanner",
    "LayerRecord",
    "Lease",
    "Mount",
    "Tag",
]

__version__ = "0.1.0"
