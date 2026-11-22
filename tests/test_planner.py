import unittest

from layergc import (
    Container,
    ImageManifest,
    LayerGCPlanner,
    LayerRecord,
    Lease,
    Mount,
    Tag,
)


def layer(digest, size=100, parent=None, corrupt=False):
    return LayerRecord(digest=digest, size=size, parent=parent, corrupt=corrupt)


class MultiTagTest(unittest.TestCase):
    def test_two_tags_one_manifest_retains_all_layers(self):
        layers = [layer("sha256:base"), layer("sha256:top", parent="sha256:base")]
        manifests = [ImageManifest("m1", ("sha256:base", "sha256:top"))]
        tags = [Tag("app:1.0", "m1"), Tag("app:latest", "m1")]
        plan = LayerGCPlanner(layers, manifests, tags).plan()
        self.assertEqual(len(plan.retained), 2)
        self.assertEqual(plan.deletable, ())
        self.assertEqual(plan.freed_bytes, 0)
        self.assertEqual(
            set(plan.reference_chains["sha256:top"]),
            {"tag app:1.0 -> manifest m1", "tag app:latest -> manifest m1"},
        )

    def test_untagged_manifest_layers_become_deletable(self):
        layers = [layer("sha256:base"), layer("sha256:top", parent="sha256:base")]
        manifests = [ImageManifest("m1", ("sha256:base", "sha256:top"))]
        plan = LayerGCPlanner(layers, manifests, tags=[]).plan()
        self.assertEqual({d.digest for d in plan.deletable},
                         {"sha256:base", "sha256:top"})
        self.assertEqual(plan.freed_bytes, 200)


class SharedLayerTest(unittest.TestCase):
    def test_shared_base_retained_while_unique_top_deleted(self):
        layers = [
            layer("sha256:base", size=500),
            layer("sha256:top-a", size=10, parent="sha256:base"),
            layer("sha256:top-b", size=20, parent="sha256:base"),
        ]
        manifests = [
            ImageManifest("m-a", ("sha256:base", "sha256:top-a")),
            ImageManifest("m-b", ("sha256:base", "sha256:top-b")),
        ]
        tags = [Tag("app:a", "m-a")]
        plan = LayerGCPlanner(layers, manifests, tags).plan()
        self.assertEqual({d.digest for d in plan.retained},
                         {"sha256:base", "sha256:top-a"})
        # top-b is unreferenced but its parent (base) is retained, so the
        # child record cannot be removed on its own: it is observed.
        self.assertEqual({d.digest for d in plan.observing}, {"sha256:top-b"})
        self.assertEqual(plan.deletable, ())
        self.assertEqual(plan.freed_bytes, 0)
        self.assertIn("sha256:top-b", plan.blocking_reasons)
        self.assertIn("parent sha256:base is kept",
                      plan.blocking_reasons["sha256:top-b"][0])

    def test_whole_unreferenced_chain_deleted_together(self):
        layers = [
            layer("sha256:base", size=500),
            layer("sha256:top-b", size=20, parent="sha256:base"),
        ]
        manifests = [ImageManifest("m-b", ("sha256:base", "sha256:top-b"))]
        plan = LayerGCPlanner(layers, manifests, tags=[]).plan()
        self.assertEqual({d.digest for d in plan.deletable},
                         {"sha256:base", "sha256:top-b"})
        self.assertEqual(plan.freed_bytes, 520)


class RunningContainerTest(unittest.TestCase):
    def test_running_container_pins_untagged_image_layers(self):
        layers = [layer("sha256:base"), layer("sha256:top", parent="sha256:base")]
        manifests = [ImageManifest("m1", ("sha256:base", "sha256:top"))]
        containers = [Container("c1", "m1", running=True)]
        plan = LayerGCPlanner(layers, manifests, containers=containers).plan()
        self.assertEqual(len(plan.retained), 2)
        self.assertEqual(plan.freed_bytes, 0)
        self.assertIn("running container c1 -> manifest m1",
                      plan.reference_chains["sha256:top"])

    def test_stopped_container_does_not_pin(self):
        layers = [layer("sha256:base")]
        manifests = [ImageManifest("m1", ("sha256:base",))]
        containers = [Container("c1", "m1", running=False)]
        plan = LayerGCPlanner(layers, manifests, containers=containers).plan()
        self.assertEqual({d.digest for d in plan.deletable}, {"sha256:base"})

    def test_container_snapshot_layer_pins_ancestors(self):
        layers = [
            layer("sha256:base"),
            layer("sha256:rw", parent="sha256:base"),
        ]
        containers = [
            Container("c1", "m-missing", running=True, layers=("sha256:rw",))
        ]
        plan = LayerGCPlanner(layers, containers=containers).plan()
        self.assertEqual({d.digest for d in plan.retained},
                         {"sha256:base", "sha256:rw"})
        self.assertIn("ancestor of sha256:rw",
                      plan.reference_chains["sha256:base"])
        self.assertTrue(plan.issues)  # unknown manifest reported


class CorruptDigestTest(unittest.TestCase):
    def test_corrupt_layer_quarantined_not_deleted(self):
        layers = [
            layer("sha256:good"),
            layer("sha256:bad", corrupt=True),
        ]
        plan = LayerGCPlanner(layers).plan()
        self.assertEqual({d.digest for d in plan.quarantined}, {"sha256:bad"})
        self.assertEqual({d.digest for d in plan.deletable}, {"sha256:good"})
        self.assertEqual(plan.freed_bytes, 100)
        self.assertIn("corrupt digest",
                      plan.blocking_reasons["sha256:bad"][0])

    def test_referenced_corrupt_layer_still_quarantined(self):
        layers = [layer("sha256:bad", corrupt=True)]
        manifests = [ImageManifest("m1", ("sha256:bad",))]
        tags = [Tag("app:broken", "m1")]
        plan = LayerGCPlanner(layers, manifests, tags).plan()
        decision = plan.quarantined[0]
        self.assertEqual(decision.digest, "sha256:bad")
        self.assertEqual(decision.referenced_by,
                         ("tag app:broken -> manifest m1",))

    def test_child_of_quarantined_layer_is_observed(self):
        layers = [
            layer("sha256:bad", corrupt=True),
            layer("sha256:child", parent="sha256:bad"),
        ]
        plan = LayerGCPlanner(layers).plan()
        self.assertEqual({d.digest for d in plan.observing}, {"sha256:child"})


class LeaseAndMountTest(unittest.TestCase):
    def test_active_lease_retains(self):
        layers = [layer("sha256:build")]
        leases = [Lease("l1", ("sha256:build",), expires_at=2000.0)]
        plan = LayerGCPlanner(layers, leases=leases).plan(now=1000.0)
        self.assertEqual({d.digest for d in plan.retained}, {"sha256:build"})
        self.assertIn("lease l1", plan.reference_chains["sha256:build"])

    def test_expired_lease_releases(self):
        layers = [layer("sha256:build")]
        leases = [Lease("l1", ("sha256:build",), expires_at=1000.0)]
        plan = LayerGCPlanner(layers, leases=leases).plan(now=1000.0)
        self.assertEqual({d.digest for d in plan.deletable}, {"sha256:build"})

    def test_temporary_mount_retains(self):
        layers = [layer("sha256:tmp")]
        mounts = [Mount("mnt1", ("sha256:tmp",))]
        plan = LayerGCPlanner(layers, mounts=mounts).plan()
        self.assertEqual({d.digest for d in plan.retained}, {"sha256:tmp"})
        self.assertIn("mount mnt1", plan.blocking_reasons["sha256:tmp"])


class ValidationTest(unittest.TestCase):
    def test_duplicate_digest_rejected(self):
        with self.assertRaises(ValueError):
            LayerGCPlanner([layer("sha256:x"), layer("sha256:x")])

    def test_unknown_digest_reported_as_issue(self):
        manifests = [ImageManifest("m1", ("sha256:ghost",))]
        tags = [Tag("app:ghost", "m1")]
        plan = LayerGCPlanner([], manifests, tags).plan()
        self.assertEqual(len(plan.issues), 1)
        self.assertIn("sha256:ghost", plan.issues[0])


if __name__ == "__main__":
    unittest.main()
