"""The shared OpenFOAM layer (foamtools): Docker commands, the credential-free child
environment, the JSON and file helpers, sim's JSON contract and the NACA geometry.
No Docker and no model are needed.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401

from foamtools import common, docker, naca
from wingstudio import IMAGE
from wingstudio.cfd import solver

SECRETS = {"ANTHROPIC_API_KEY": "k1", "GITHUB_TOKEN": "k2", "DB_PASSWORD": "k3", "APP_SECRET": "k4"}


class Docker(unittest.TestCase):
    def test_command_mounts_the_root_and_runs_in_the_directory(self):
        argv = docker.command(["simpleFoam"], cwd="cases/x", name="st-c1-1", image_name="img:1")
        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        self.assertIn(f"{common.ROOT}:/work", argv)
        self.assertEqual(argv[argv.index("--name") + 1], "st-c1-1")
        self.assertEqual(argv[argv.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(argv[-3:], ["of", "cases/x", "simpleFoam"])
        self.assertIn("img:1", argv)
        self.assertNotIn("-t", argv)

    def test_the_image_comes_from_of_image_or_the_default(self):
        with mock.patch.dict(os.environ, {"OF_IMAGE": "custom:1"}):
            self.assertIn("custom:1", docker.command(["true"]))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn(docker.DEFAULT_IMAGE, docker.command(["true"]))

    def test_children_never_get_credentials(self):
        with mock.patch.dict(os.environ, dict(SECRETS, OF_IMAGE="img:2")), \
                mock.patch.object(docker, "ensure_running"), \
                mock.patch.object(subprocess, "Popen") as popen, mock.patch.object(subprocess, "run") as run:
            docker.popen(["true"], name="n")
            docker.cli("ps")
        for call in (popen.call_args, run.call_args):
            env = call.kwargs["env"]
            self.assertFalse(set(SECRETS) & set(env), env.keys())
            self.assertEqual(env["OF_IMAGE"], "img:2")

    def test_popen_starts_docker_first_and_reports_when_it_cannot(self):
        with mock.patch.object(docker, "ensure_running", side_effect=docker.DockerError("down")), \
                mock.patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(docker.DockerError):
                docker.popen(["true"])
        popen.assert_not_called()

    def test_the_solver_starts_through_popen_with_the_studio_image(self):
        case_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, case_dir, True)
        run = solver.SolverRun("c1", case_dir, lambda *a, **k: None, 100, 0)
        with mock.patch.object(docker, "popen") as popen, mock.patch.object(solver.threading, "Thread"):
            run.start()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["simpleFoam"])
        self.assertEqual((kwargs["name"], kwargs["image_name"]), (run.container, IMAGE))

    def test_leftover_containers_are_those_of_the_named_cases(self):
        listing = subprocess.CompletedProcess([], 0, "st-a-1\nst-a-12\nst-ab-1\nst-b-3\nxfoil-1\nst-a-x\n", "")
        with mock.patch.object(docker, "cli", return_value=listing):
            self.assertEqual(solver.leftover_solver_containers("a"), ["st-a-1", "st-a-12"])
            self.assertEqual(solver.leftover_solver_containers("a", "b"), ["st-a-1", "st-a-12", "st-b-3"])
        self.assertEqual(solver.leftover_solver_containers(), [])
        with mock.patch.object(docker, "cli", return_value=subprocess.CompletedProcess([], 1, "", "no docker")):
            self.assertEqual(solver.leftover_solver_containers("a"), [])


class Common(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_clean_env_drops_credentials_and_adds_extra(self):
        with mock.patch.dict(os.environ, dict(SECRETS, KEEP="1")):
            env = common.clean_env(EXTRA="2")
        self.assertEqual((env["KEEP"], env["EXTRA"]), ("1", "2"))
        self.assertFalse(set(SECRETS) & set(env))

    def test_json_files(self):
        path = os.path.join(self.dir, "a.json")
        self.assertEqual(common.read_json(path, {"none": 1}), {"none": 1})
        common.write_json(path, {"x": [1, 2]})
        self.assertEqual(common.read_json(path), {"x": [1, 2]})
        self.assertEqual(os.listdir(self.dir), ["a.json"])            # no temporary file left behind
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(common.read_json(path))
        self.assertEqual(common.read_text(os.path.join(self.dir, "missing")), "")

    def test_load_env_keeps_what_is_set(self):
        path = os.path.join(self.dir, ".env")
        with open(path, "w") as f:
            f.write("# comment\nA_ONE=1\nA_TWO='two'\nnot a line\nA_SET=file\n")
        with mock.patch.dict(os.environ, {"A_SET": "env"}):
            common.load_env(path)
            self.assertEqual((os.environ["A_ONE"], os.environ["A_TWO"], os.environ["A_SET"]), ("1", "two", "env"))

    def test_numbers(self):
        self.assertEqual(common.round_sig(0.0123456, 3), 0.0123)
        self.assertIsNone(common.round_sig(float("nan")))
        self.assertEqual(common.round_sig("text"), "text")
        self.assertTrue(common.is_finite(1.5))
        self.assertFalse(any(common.is_finite(v) for v in (True, None, "1", float("inf"), float("nan"))))

    def test_time_dirs(self):
        for name in ("0", "0.orig", "100", "25.5", "constant"):
            os.makedirs(os.path.join(self.dir, name))
        open(os.path.join(self.dir, "300"), "w").close()                 # a file is not a time
        self.assertEqual(common.time_dirs(self.dir), [(0.0, "0"), (25.5, "25.5"), (100.0, "100")])
        self.assertEqual(common.time_dirs(os.path.join(self.dir, "missing")), [])

    def test_run_sim_answers_json_also_when_sim_does_not(self):
        broken = subprocess.CompletedProcess([], 1, "Traceback ...", "boom")
        with mock.patch.object(subprocess, "run", return_value=broken) as run:
            out = common.run_sim("info", "x", image="img:3")
        self.assertEqual((out["ok"], out["error"]), (False, "sim produced no JSON"))
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(argv[1:], ["-m", "foamtools.sim", "info", "x"])
        self.assertEqual((kwargs["cwd"], kwargs["env"]["OF_IMAGE"]), (common.ROOT, "img:3"))
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("sim", 5)):
            self.assertFalse(common.run_sim("mesh", "x", timeout=5)["ok"])


class Naca(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(naca.naca4("NACA 4412"), ("4412", 0.04, 0.4, 0.12))
        with self.assertRaises(ValueError):
            naca.naca4("23012")

    def test_the_profile_is_closed_and_as_thick_as_its_code(self):
        upper, lower = naca.naca4_coords("0012", 200)
        self.assertEqual((upper[0], lower[0]), ((0.0, 0.0), (0.0, 0.0)))
        self.assertAlmostEqual(upper[-1][1], 0.0, places=12)
        self.assertAlmostEqual(lower[-1][1], 0.0, places=12)
        thickness = max(u[1] - l[1] for u, l in zip(upper, lower))
        self.assertAlmostEqual(thickness, 0.12, delta=0.001)

    def test_obj_replaces_the_old_surface(self):
        case = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, case, True)
        naca.write_obj(case, "2412", n=50)
        naca.write_obj(case, "4415", n=50)
        folder = os.path.join(case, "constant", "geometry")
        self.assertEqual(os.listdir(folder), ["NACA4415.obj"])
        with open(os.path.join(folder, "NACA4415.obj")) as f:
            lines = f.read().splitlines()
        loop = 2 * 50                                   # 51 upper points and 49 lower ones between the ends
        self.assertEqual(sum(line.startswith("v ") for line in lines), 2 * loop)
        self.assertEqual(sum(line.startswith("f ") for line in lines), 2 * loop)


if __name__ == "__main__":
    unittest.main()
