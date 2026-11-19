"""The app as a whole: the local-only guard, the config route, the static pages, and a start
without a reference catalogue. The plan, metrics and reference routes are tested with their
subjects (test_plan_edits, test_metrics, test_refs).
"""
import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

import support
from fastapi.testclient import TestClient

from wingstudio import server


def without_key():
    return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}, clear=True)


class App(support.ServerCase):
    def test_local_only_guard_covers_every_route(self):
        for path in ("/api/refs", "/api/refs/runs?setting=mesh.n_normal&study=x&case=y", "/api/config",
                     "/api/metrics/summary"):
            for headers in ({"host": "evil.example"}, {"sec-fetch-site": "cross-site"},
                            {"origin": "https://evil.example"}):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 403, (path, headers))
            self.assertEqual(self.client.get(path, headers={"origin": "http://localhost:8800"}).status_code, 200, path)

    def test_the_reference_page_is_static(self):
        for name in ("ref.html", "js/ref.js"):
            self.assertEqual(self.client.get("/" + name).status_code, 200)

    def test_without_a_catalogue_the_app_still_starts_and_answers(self):
        support.use_catalogue(self, missing=True)
        with redirect_stderr(io.StringIO()):
            client = TestClient(server.create_app(data_dir=self.data, viewer_factory=None), base_url=support.BASE)
            body = client.get("/api/refs").json()
            self.assertEqual((body["entries"], body["defaults"]), ([], {}))
            self.assertIn("could not be read", body["error"])
            self.assertEqual(client.get("/api/config").status_code, 200)

    def test_llm_ready_is_a_boolean_and_never_the_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-value-not-a-key"}):
            r = self.client.get("/api/config")
            self.assertIs(r.json()["llm_ready"], True)
            self.assertNotIn("test-value-not-a-key", r.text)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertIs(self.client.get("/api/config").json()["llm_ready"], False)
        with without_key():
            body = self.client.get("/api/config").json()
        self.assertIs(body["llm_ready"], False)
        self.assertEqual(set(body), {"models", "presets", "default_project_id", "stage_defs", "llm_ready"})

    def test_an_injected_model_is_ready_without_a_key(self):
        app = server.create_app(data_dir=self.data, viewer_factory=None, completion=lambda **kwargs: None)
        with without_key():
            self.assertIs(TestClient(app, base_url=support.BASE).get("/api/config").json()["llm_ready"], True)


if __name__ == "__main__":
    unittest.main()
