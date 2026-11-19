"""Wall edges of the C-mesh: projected where that is safe, explicit where it is not.

blockMesh's `project` edge starts from the straight line between two wall vertices and moves
each point to the nearest point of the whole profile. On a thin, strongly cambered section
that line runs on the far side of the mean line near the trailing edge, so points of the upper
edge land on the lower surface (NACA 5412: 67 negative-volume cells). write_mesh_dict writes
such an edge as a polyLine through the profile and leaves every other edge as the template has
it. No Docker and no model are needed.
"""
import hashlib
import os
import re
import shutil
import tempfile
import unittest

import support  # noqa: F401

from wingstudio.cfd import case as ac

PROJECTED = ("project 4 7 (aerofoil)", "project 7 5 (aerofoil)", "project 4 8 (aerofoil)", "project 8 5 (aerofoil)")


def mesh_dict(code):
    d = tempfile.mkdtemp(prefix="meshdict_")
    try:
        ac.write_mesh_dict(d, code)
        with open(os.path.join(d, "system", "blockMeshDict")) as f:
            return f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


class CleanSections(unittest.TestCase):
    """Sections that mesh today keep the template's projected edges, so their meshes and the
    reference numbers measured on them do not move."""

    def test_the_reference_section_and_those_of_the_recorded_studies_stay_projected(self):
        # symmetric sections never switch
        for code in ("4412", "2412", "4415", "5415", "2408", "4414", "3414", "2415", "3415", "3410",
                     "0006", "0012", "0024"):
            with self.subTest(code=code):
                text = mesh_dict(code)
                self.assertNotIn("polyLine", text)
                for edge in PROJECTED:
                    self.assertIn(edge, text)

    def test_reference_dictionary_is_pinned(self):
        # NACA 4412 passes the projection test with a small margin. If this hash changes, the
        # reference case (Cl 0.886, Cd 0.0131) is no longer built from the same dictionary:
        # re-run it before accepting the new value.
        digest = hashlib.sha256(mesh_dict("4412").encode()).hexdigest()
        self.assertEqual(digest[:16], REFERENCE_4412)


class CrossingSections(unittest.TestCase):
    def test_thin_cambered_section_gets_an_explicit_upper_aft_edge(self):
        text = mesh_dict("5412")
        self.assertFalse(ac.projection_is_clean("5412", ac.X_SPLIT, 1.0, True))
        for v0, v1 in ((8, 5), (20, 17)):                      # both planes of the 2D slab
            self.assertRegex(text, rf"polyLine {v0} {v1}\s*\(")
            self.assertNotIn(f"project {v0} {v1} (aerofoil)", text)
        for edge in PROJECTED[:3]:                             # the other three stay projected
            self.assertIn(edge, text)

    def test_forward_camber_gets_an_explicit_upper_leading_edge(self):
        text = mesh_dict("4212")
        self.assertRegex(text, r"polyLine 4 8\s*\(")
        self.assertRegex(text, r"polyLine 16 20\s*\(")

    def test_polyline_points_lie_on_the_upper_surface_in_order(self):
        text = mesh_dict("5412")
        body = re.search(r"polyLine 8 5\s*\(\s*(.*?)\n    \)", text, re.S).group(1)
        pts = [tuple(float(v) for v in m.groups()) for m in re.finditer(r"\(([-\d.e]+) ([-\d.e]+) ([-\d.e]+)\)", body)]
        self.assertEqual(len(pts), 199)
        xs = [p[0] for p in pts]
        self.assertEqual(xs, sorted(xs))                       # from the 30 % point to the trailing edge
        # strictly between the two vertices (a surface point is offset from its chord station
        # along the normal of the mean line, so the 30 % vertex is not at x = 0.3)
        x_start, x_end = ac.naca4_point("5412", ac.X_SPLIT)[0][0], ac.naca4_point("5412", 1.0)[0][0]
        self.assertTrue(x_start < xs[0] and xs[-1] < x_end)
        for x, y, z in pts[::20]:
            self.assertEqual(y, -0.1)
            (xu, zu), _ = ac.naca4_point("5412", x)
            # the stored point is a profile point: the upper surface at its own x is within rounding
            self.assertAlmostEqual(z, zu, delta=2e-3)
            self.assertGreater(z, 0.0)


REFERENCE_4412 = "1e70ca69c3a7bc69"


if __name__ == "__main__":
    unittest.main()
