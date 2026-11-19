"""The reference catalogue (wingstudio/refs): the catalogue and its queries, the local documents
and their passages, the lint, the files in the repository, earlier runs of similar cases, and
the /api/refs routes. The catalogue, the documents and the store are temporary unless a test
reads the real files: nothing under wingstudio/refs, studio_data/ or cases/ is written.
"""
import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from support import HAIKU, SONNET, citations

from wingstudio import runtime
from wingstudio.study import mission, planning

QUOTE = "Employs upwind interpolation weights, with an explicit correction based on the local cell gradient"
MARKDOWN = f"""# OpenFOAM documentation: Linear-upwind divergence scheme

Source: https://example.org/linearUpwind/
Retrieved: 2026-11-18
Licence: "Copyright © OpenCFD Ltd." (footer of the page)
Scope: excerpts relevant to Wing Studio

## Properties

- {QUOTE}
- Second order
- Unbounded
"""
INDEX = {
    "id": "openfoam-linearupwind", "title": "OpenFOAM documentation: Linear-upwind divergence scheme",
    "source_url": "https://example.org/openfoam-linearupwind", "retrieved": "2026-11-18",
    "licence": '"Copyright © OpenCFD Ltd." (footer of the page)', "scope": "excerpts", "raw_file": "page.html",
    "passages": [
        {"key": "upwind-weights", "heading": "Properties", "text": QUOTE,
         "settings": ["numerics.convection_scheme"], "why": "Upwind interpolation plus a gradient correction."},
        {"key": "second-order", "heading": "Properties", "text": "Second order",
         "settings": ["numerics.convection_scheme"], "why": "The page classes the scheme as second order.",
         "raw_file": "page.html"}],
}
DIFFERS = [{"settings": ["numerics.convection_scheme"],
            "text": "The page calls the scheme unbounded. The studio's agent text once called it bounded."}]
SOUND = {"openfoam-linearupwind.md": MARKDOWN, "openfoam-linearupwind.json": INDEX}


def use_docs(test, docs, catalogue=None):
    """A temporary catalogue with a docs/ folder beside it. `docs` maps a file name to its text
    (a dict is written as JSON)."""
    path = support.use_catalogue(test, catalogue)
    folder = os.path.join(os.path.dirname(path), "docs")
    os.makedirs(folder)
    for name, content in docs.items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
    return folder


def with_differs():
    cat = support.catalogue()
    next(e for e in cat["entries"] if e["id"] == "openfoam-linearupwind")["differs"] = copy.deepcopy(DIFFERS)
    return cat


def entry(ref_id):
    return next(e for e in citations.public()["entries"] if e["id"] == ref_id)


def lint():
    return "\n".join(citations.problems(planning.plan_setting_names()))


class CatalogueModule(unittest.TestCase):
    def test_load_and_queries(self):
        support.use_catalogue(self)
        data = citations.load()
        self.assertIsNone(data["error"])
        self.assertEqual(len(data["order"]), 27)
        self.assertTrue(citations.exists("menter-1994"))
        self.assertFalse(citations.exists("menter-1993"))
        self.assertFalse(citations.exists(None))
        self.assertEqual(citations.get("menter-1994")["kind"], "paper")
        self.assertIsNone(citations.get("nope"))
        self.assertEqual(citations.default_cites("mesh", "wake_chords"), ["project-farfield-30"])
        self.assertEqual(citations.default_cites("post", "field"), [])
        self.assertEqual(set(citations.default_section("solve")), {"extend_iterations"})
        citations.default_cites("mesh", "wake_chords").append("x")     # a copy: the cache is not touched
        self.assertEqual(citations.default_cites("mesh", "wake_chords"), ["project-farfield-30"])

    def test_for_stage_follows_the_settings(self):
        support.use_catalogue(self)
        self.assertEqual(citations.ids_for("solve"), ["project-reference-4412", "project-gate-limits"])
        self.assertEqual(citations.ids_for("post"), [])
        self.assertIn("drela-1989-xfoil", citations.ids_for("physics"))      # the angle is applied by physics
        self.assertNotIn("drela-1989-xfoil", citations.ids_for("mesh"))
        self.assertEqual(set(citations.for_stage("mesh")[0]), {"id", "kind", "title", "says", "cite_for"})

    def test_clean(self):
        self.assertEqual(citations.clean("a"), (["a"], None))
        self.assertEqual(citations.clean([" a ", "b", "a", ""]), (["a", "b"], None))
        self.assertEqual(citations.clean([]), ([], None))
        for bad in (5, {"a": 1}, ["a", 3], None):
            self.assertEqual(citations.clean(bad), (None, "shape"))

    def test_public_blanks_unverified_addresses(self):
        cat = support.catalogue()
        cat["entries"][0].update(url="https://example.org/unchecked", url_verified=False)
        cat["entries"][1].update(url="http://example.org/plain", url_verified=True)
        support.use_catalogue(self, cat)
        body = citations.public()
        self.assertEqual(set(body), {"version", "checked", "error", "entries", "defaults"})
        self.assertEqual(body["defaults"], support.DEFAULTS)
        self.assertEqual([e["id"] for e in body["entries"]], [e["id"] for e in cat["entries"]])
        for e in body["entries"][:2]:
            self.assertEqual((e["url"], e["url_verified"]), ("", False))
        self.assertEqual(set(body["entries"][0]), {"id", "title", "kind", "source", "says", "settings", "locator",
                                                   "checked", "url", "url_verified", "doc", "scope", "licence",
                                                   "retrieved", "passages", "differs"})
        self.assertTrue(body["entries"][2]["url"].startswith("https://"))
        for e in body["entries"]:
            self.assertEqual(bool(e["url"]), e["url_verified"])
        self.assertTrue(all(e["url"] == "" for e in body["entries"] if e["kind"].startswith("project-")))

    def test_missing_or_broken_catalogue_is_an_error_not_a_crash(self):
        for kwargs in ({"missing": True}, {"content": "{not json"}, {"content": {"entries": "no"}}):
            support.use_catalogue(self, **kwargs)
            with redirect_stderr(io.StringIO()):
                data = citations.load()
            self.assertTrue(data["error"])
            self.assertEqual((data["entries"], data["defaults"]), ({}, {}))
            body = citations.public()
            self.assertEqual((body["entries"], body["defaults"], body["error"]), ([], {}, data["error"]))
            self.assertIsNone(citations.document("openfoam-linearupwind"))
            errors = citations.check("NACA 2412: mesh.cites.n_normal", ["project-gate-limits"], "mesh", "tail")
            self.assertEqual(errors, [
                "NACA 2412: mesh.cites.n_normal: reference id 'project-gate-limits' cannot be checked: the studio's "
                f"reference catalogue failed to load ({data['error']}). Leave cites out."])

    def test_an_edit_of_the_file_is_seen_without_a_restart(self):
        path = support.use_catalogue(self)
        self.assertTrue(citations.exists("menter-1994"))
        cat = support.catalogue()
        cat["entries"] = [e for e in cat["entries"] if e["id"] != "menter-1994"]
        with open(path, "w") as f:
            json.dump(cat, f)
        os.utime(path, ns=(1, 1))                         # whatever the clock resolution, the key changes
        self.assertFalse(citations.exists("menter-1994"))

    def test_a_default_that_names_no_entry_is_left_out_and_linted(self):
        cat = support.catalogue()
        cat["defaults"]["mesh.n_wake"] = ["no-such-entry"]
        cat["defaults"]["mesh.bogus"] = ["project-gate-limits"]
        cat["entries"][0]["kind"] = "blog"
        cat["entries"][1]["id"] = "Menter_1994"
        cat["entries"][2]["url_verified"], cat["entries"][2]["url"] = True, ""
        cat["entries"].append(dict(cat["entries"][3]))
        support.use_catalogue(self, cat)
        self.assertEqual(citations.default_cites("mesh", "n_wake"), [])
        text = lint()
        for part in ("names 'no-such-entry', which is not an entry", "defaults['mesh.bogus'] is not a plan setting",
                     "kind must be one of", "id must be lower-case words", "url_verified may be true only with a url",
                     "the id is used twice"):
            self.assertIn(part, text)

    def test_command_line(self):
        support.use_catalogue(self)
        with redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(citations.main(["--lint"]), 0)
        self.assertIn("27 entries, 0 problem(s)", printed.getvalue())
        cat = support.catalogue()
        cat["entries"][0]["says"] = ""
        support.use_catalogue(self, cat)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(citations.main(["--lint"]), 1)


class Documents(unittest.TestCase):
    """citations.public() says which entries have a local document and quotes its passages;
    citations.document() returns one document."""

    def docs(self, extra=None, catalogue=None):
        return use_docs(self, dict(SOUND, **(extra or {})), with_differs() if catalogue is None else catalogue)

    def test_public_describes_the_document_of_an_entry(self):
        self.docs()
        e = entry("openfoam-linearupwind")
        self.assertEqual((e["doc"], e["scope"], e["retrieved"]), (True, "excerpts", "2026-11-18"))
        self.assertEqual(e["licence"], INDEX["licence"])
        self.assertEqual(e["passages"], [
            {"key": "upwind-weights", "heading": "Properties", "text": QUOTE,
             "settings": ["numerics.convection_scheme"], "why": "Upwind interpolation plus a gradient correction."},
            {"key": "second-order", "heading": "Properties", "text": "Second order",
             "settings": ["numerics.convection_scheme"], "why": "The page classes the scheme as second order."}])
        self.assertEqual(e["differs"], DIFFERS)
        self.assertEqual(e["url"], "https://example.org/openfoam-linearupwind")

    def test_an_entry_without_a_document(self):
        self.docs()
        e = entry("menter-1994")
        self.assertEqual((e["doc"], e["scope"], e["licence"], e["retrieved"], e["passages"], e["differs"]),
                         (False, "", "", "", [], []))
        # no docs folder at all is the same
        support.use_catalogue(self)
        e = entry("openfoam-linearupwind")
        self.assertEqual((e["doc"], e["passages"], e["differs"]), (False, [], []))

    def test_only_what_stands_in_the_document_is_a_quotation(self):
        odd = dict(INDEX, passages=[{"key": "spaced", "heading": "H", "text": "- Second order\n", "settings": [],
                                     "why": "w"}])
        self.docs({"openfoam-linearupwind.json": odd})
        self.assertEqual(entry("openfoam-linearupwind")["passages"][0]["text"], "- Second order\n")   # untouched
        index = copy.deepcopy(INDEX)
        index["passages"][0]["text"] = QUOTE.replace("explicit", "implicit")       # reworded
        index["passages"].append({"key": "ours", "heading": "", "text": "second order", "settings": [], "why": ""})
        folder = self.docs({"openfoam-linearupwind.json": index})
        self.assertEqual([p["key"] for p in entry("openfoam-linearupwind")["passages"]], ["second-order"])
        # the same index quotes the document again once the document says so
        path = os.path.join(folder, "openfoam-linearupwind.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(MARKDOWN.replace("explicit", "implicit"))
        os.utime(path, ns=(1, 1))
        self.assertEqual([p["key"] for p in entry("openfoam-linearupwind")["passages"]],
                         ["upwind-weights", "second-order"])
        # what public() hands out is a copy: the cache is not touched
        entry("openfoam-linearupwind")["passages"][0]["settings"].append("x")
        self.assertEqual(entry("openfoam-linearupwind")["passages"][0]["settings"], ["numerics.convection_scheme"])

    def test_an_index_that_cannot_be_used_leaves_the_document(self):
        for broken in ("{not json", "[]", {"passages": "none"},
                       {"passages": [1, {"key": "k"}, {"text": "t"}, {"key": 3, "text": "t"}], "licence": 5}):
            self.docs({"openfoam-linearupwind.json": broken})
            e = entry("openfoam-linearupwind")
            self.assertEqual((e["doc"], e["passages"], e["licence"]), (True, [], ""), broken)
            self.assertEqual(citations.document("openfoam-linearupwind")["markdown"], MARKDOWN)
        # an index without its document is not a document, and quotes nothing
        use_docs(self, {"openfoam-linearupwind.json": INDEX})
        e = entry("openfoam-linearupwind")
        self.assertEqual((e["doc"], e["passages"], e["retrieved"]), (False, [], "2026-11-18"))
        self.assertIsNone(citations.document("openfoam-linearupwind"))

    def test_differs_keeps_only_what_has_a_text(self):
        cat = support.catalogue()
        cat["entries"][0]["differs"] = [{"settings": ["physics.nut_ratio", 7], "text": " One sentence. "},
                                        {"settings": [], "text": ""}, "odd", {"text": 3}]
        cat["entries"][1]["differs"] = "not a list"
        support.use_catalogue(self, cat)
        body = citations.public()
        self.assertEqual(body["entries"][0]["differs"], [{"settings": ["physics.nut_ratio"], "text": "One sentence."}])
        self.assertEqual(body["entries"][1]["differs"], [])

    def test_document(self):
        self.docs({"menter-1994.md": "# Text\n"})
        doc = citations.document("openfoam-linearupwind")
        self.assertEqual(doc, {"id": "openfoam-linearupwind", "title": INDEX["title"], "markdown": MARKDOWN,
                               "source_url": "https://example.org/openfoam-linearupwind", "licence": INDEX["licence"],
                               "retrieved": "2026-11-18", "scope": "excerpts"})
        self.assertIn("©", doc["markdown"])
        # without an index: the catalogue's title
        self.assertEqual(citations.document("menter-1994"), {
            "id": "menter-1994", "title": "Title of menter-1994", "markdown": "# Text\n",
            "source_url": "https://example.org/menter-1994", "licence": "", "retrieved": "", "scope": ""})

    def test_the_address_is_the_catalogue_s_and_only_when_verified(self):
        # the index is not a second authority for an address
        for other in ("https://elsewhere.example/page", "http://example.org/plain", ["https://example.org/a"], ""):
            self.docs({"openfoam-linearupwind.json": dict(INDEX, source_url=other)})
            self.assertEqual(citations.document("openfoam-linearupwind")["source_url"],
                             "https://example.org/openfoam-linearupwind", other)
        for change in ({"url_verified": False}, {"url": "http://example.org/plain"}, {"url": ""},
                       {"url_verified": "yes"}):
            cat = with_differs()
            next(e for e in cat["entries"] if e["id"] == "openfoam-linearupwind").update(change)
            self.docs(catalogue=cat)
            self.assertEqual(entry("openfoam-linearupwind")["url"], "", change)
            self.assertEqual(citations.document("openfoam-linearupwind")["source_url"], "", change)

    def test_a_symbolic_link_is_never_followed(self):
        # only wingstudio/refs/docs/<id>.md itself is ever read
        folder = self.docs()
        outside = os.path.join(os.path.dirname(folder), "outside.txt")
        with open(outside, "w") as f:
            f.write("a file outside docs")
        os.symlink(outside, os.path.join(folder, "menter-1994.md"))
        os.symlink(os.path.join(folder, "openfoam-linearupwind.json"), os.path.join(folder, "menter-1994.json"))
        self.assertIsNone(citations.document("menter-1994"))
        self.assertFalse(citations.has_document("menter-1994"))
        e = entry("menter-1994")
        self.assertEqual((e["doc"], e["passages"], e["retrieved"]), (False, [], ""))
        found = lint()
        self.assertIn("docs/menter-1994.md: is a symbolic link, which is never read", found)
        self.assertIn("docs/menter-1994.json: is a symbolic link, which is never read", found)
        # the real document beside them is served as before, also when the folder itself is a link
        self.assertEqual(citations.document("openfoam-linearupwind")["markdown"], MARKDOWN)
        os.rename(folder, folder + "-real")
        os.symlink(folder + "-real", folder)
        self.assertEqual(citations.document("openfoam-linearupwind")["markdown"], MARKDOWN)

    def test_doc_is_true_only_for_a_document_the_route_can_serve(self):
        # bytes that are not UTF-8 gave doc true and a 404 from the route
        folder = self.docs()
        with open(os.path.join(folder, "xfoil-primer.md"), "wb") as f:
            f.write(b"caf\xe9 \xff\xfe")
        os.mkdir(os.path.join(folder, "menter-1994.md"))                  # a folder is not a document
        for ref_id in ("xfoil-primer", "menter-1994"):
            self.assertIsNone(citations.document(ref_id))
            self.assertFalse(entry(ref_id)["doc"], ref_id)
        found = lint()
        self.assertIn("docs/xfoil-primer.md: could not be read as UTF-8 text, so it is not served", found)
        self.assertIn("docs/menter-1994.md: could not be read as UTF-8 text, so it is not served", found)
        # repaired: seen without a restart
        path = os.path.join(folder, "xfoil-primer.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("primer text, longer than before")
        self.assertTrue(entry("xfoil-primer")["doc"])
        self.assertEqual(citations.document("xfoil-primer")["markdown"], "primer text, longer than before")

    def test_only_documents_of_catalogue_ids_are_read(self):
        folder = self.docs({"stray.md": "not in the catalogue"})
        with open(os.path.join(os.path.dirname(folder), "secret.md"), "w") as f:
            f.write("outside docs")
        for bad in ("Openfoam-Linearupwind", "../secret", "..", "", "openfoam-linearupwind.md", "docs/stray",
                    "openfoam-linearupwind/", "a" * 65, None, 5, ["openfoam-linearupwind"], "nope",
                    "openfoam-linearupwind\n", "menter-1994"):
            self.assertIsNone(citations.document(bad), repr(bad))
            self.assertFalse(citations.has_document(bad), repr(bad))
        # a file that is there under an id that is not an entry: still nothing, and public() never lists it
        self.assertTrue(citations.has_document("stray"))
        self.assertIsNone(citations.document("stray"))
        self.assertNotIn("stray", [e["id"] for e in citations.public()["entries"]])

    def test_an_edit_of_the_index_is_seen_without_a_restart(self):
        folder = self.docs()
        self.assertEqual(len(entry("openfoam-linearupwind")["passages"]), 2)
        path = os.path.join(folder, "openfoam-linearupwind.json")
        with open(path, "w") as f:
            json.dump(dict(INDEX, passages=INDEX["passages"][:1]), f)
        os.utime(path, ns=(1, 1))
        self.assertEqual(len(entry("openfoam-linearupwind")["passages"]), 1)
        os.remove(path)
        self.assertEqual(entry("openfoam-linearupwind")["passages"], [])


class Lint(unittest.TestCase):
    def problems(self, docs, catalogue=None):
        use_docs(self, docs, catalogue)
        return lint()

    def test_sound_documents_are_clean(self):
        self.assertEqual(self.problems(dict(SOUND, **{"menter-1994.md": "a document without an index",
                                                      ".DS_Store": "x", "notes.txt": "ignored"}), with_differs()), "")

    def test_a_quotation_must_stand_in_the_document(self):
        index = copy.deepcopy(INDEX)
        index["passages"][0]["text"] = QUOTE.replace("explicit", "implicit")
        index["passages"][1]["key"] = "Second Order"
        index["passages"].append({"key": "upwind-weights", "text": "Unbounded", "settings": []})
        index["passages"].append({"key": "empty", "text": " ", "settings": []})
        index["passages"].append({"key": "odd-setting", "text": "Unbounded", "settings": ["numerics.bogus"]})
        index["passages"].append({"key": "no-settings", "text": "Unbounded"})
        text = self.problems({"openfoam-linearupwind.md": MARKDOWN, "openfoam-linearupwind.json": index})
        for part in ("docs/openfoam-linearupwind.json: passage 1 ('upwind-weights'): text is not found word for word "
                     "in openfoam-linearupwind.md",
                     "passage 2 ('Second Order'): key must be lower-case words joined by hyphens, used once",
                     "passage 3 ('upwind-weights'): key must be lower-case words",
                     "passage 4 ('empty'): text must be a quotation",
                     "passage 5 ('odd-setting'): settings names numerics.bogus, which is not a plan setting",
                     "passage 6 ('no-settings'): settings must be a list of 'stage.setting' names"):
            self.assertIn(part, text)

    def test_files_must_belong_to_an_entry(self):
        text = self.problems({"stray.md": "x", "stray.json": {"id": "stray", "passages": []},
                              "menter-1994.json": dict(INDEX, id="menter-1994"),          # no menter-1994.md
                              "xfoil-primer.md": "text", "xfoil-primer.json": {"id": "other", "passages": [],
                                                                               "source_url": "http://plain"},
                              "naca-tr-460.md": "text", "naca-tr-460.json": "{"})
        for part in ("docs/stray.md: 'stray' is not an entry of the catalogue",
                     "docs/stray.json: 'stray' is not an entry of the catalogue",
                     "docs/menter-1994.json: the index or its document menter-1994.md could not be read",
                     "docs/xfoil-primer.json: id must be 'xfoil-primer'",
                     "docs/xfoil-primer.json: source_url must be empty or start with https://",
                     "docs/naca-tr-460.json: the index or its document naca-tr-460.md could not be read"):
            self.assertIn(part, text)

    def test_the_index_names_the_address_of_the_catalogue_or_none(self):
        docs = {"openfoam-linearupwind.md": MARKDOWN,
                "openfoam-linearupwind.json": dict(INDEX, source_url="https://elsewhere.example/page")}
        self.assertEqual(self.problems(docs), "docs/openfoam-linearupwind.json: source_url must be the url of the "
                                              "catalogue entry (https://example.org/openfoam-linearupwind)")
        docs["openfoam-linearupwind.json"] = dict(INDEX, source_url="")
        self.assertEqual(self.problems(docs), "")
        docs = {"white-fluid-mechanics.md": "text",                      # a book: the catalogue has no address
                "white-fluid-mechanics.json": {"id": "white-fluid-mechanics", "passages": [],
                                               "source_url": "https://example.org/white"}}
        self.assertIn("source_url must be the url of the catalogue entry (which has none)", self.problems(docs))

    def test_differs_shape(self):
        cat = support.catalogue()
        cat["entries"][0]["differs"] = "text"
        cat["entries"][1]["differs"] = [{"settings": ["physics.turbulence_model"], "text": ""},
                                        {"settings": ["physics.bogus"], "text": "t"},
                                        {"settings": "physics.nut_ratio", "text": "t"},
                                        {"settings": ["physics.turbulence_model"], "text": "A sound one."}]
        support.use_catalogue(self, cat)
        self.assertEqual(citations.problems(planning.plan_setting_names()), [
            "entry 1 ('spalart-allmaras-1992'): differs must be a list of {settings, text}",
            "entry 2 ('menter-1994'): differs 1 needs a text",
            "entry 2 ('menter-1994'): differs 2: settings names physics.bogus, which is not a plan setting",
            "entry 2 ('menter-1994'): differs 3: settings must be a list of 'stage.setting' names"])


class RealFiles(unittest.TestCase):
    """wingstudio/refs/catalogue.json and wingstudio/refs/docs as they are in the repository."""

    def setUp(self):
        support.use_real_catalogue(self)

    def test_lint_is_clean(self):
        self.assertEqual(citations.problems(planning.plan_setting_names()), [])

    def test_every_setting_is_cited_or_listed_as_uncited(self):
        defaults = citations.load()["defaults"]
        for name in sorted(planning.plan_setting_names()):
            self.assertTrue((name in defaults) != (name in support.UNCITED),
                            f"{name}: give it a default citation or add it to the uncited list, not both")

    def test_every_cited_setting_has_a_default_reason(self):
        case = planning.default_case_plan(support.make_study(), {"naca": "2412", "xfoil": {"alpha_design": 2.5}})
        for key in citations.load()["defaults"]:
            stage, setting = key.split(".", 1)
            if stage != "case":
                self.assertTrue(case["stages"][stage]["reasons"].get(setting), key)

    def test_the_test_catalogue_mirrors_the_real_one(self):
        # sizes measured on the temporary catalogue of the other tests are only those of the
        # real one while it lists the same references for the same settings
        raw = support.read_json(support.REAL_CATALOGUE)
        self.assertEqual({e["id"]: (e["kind"], e["settings"]) for e in raw["entries"]}, support._ENTRIES,
                         "tests/support.py _ENTRIES is not the ids, kinds and settings of refs/catalogue.json")
        self.assertEqual(raw["defaults"], support.DEFAULTS,
                         "tests/support.py DEFAULTS is not the defaults of refs/catalogue.json")
        self.assertEqual([e["id"] for e in support.catalogue()["entries"]], [e["id"] for e in raw["entries"]])

    def test_every_document_is_complete(self):
        entries = [e for e in citations.public()["entries"] if e["doc"]]
        self.assertTrue(entries, "wingstudio/refs/docs has no documents")
        for e in entries:
            doc = citations.document(e["id"])
            self.assertTrue(doc["markdown"].strip(), e["id"])
            self.assertTrue(doc["source_url"].startswith("https://"), e["id"])
            self.assertTrue(e["retrieved"] and e["licence"] and e["scope"] and e["passages"], e["id"])
            for p in e["passages"]:
                self.assertIn(p["text"], doc["markdown"], (e["id"], p["key"]))
                self.assertTrue(p["why"] and p["settings"], (e["id"], p["key"]))

    def test_passages_and_differs_stay_inside_their_entry(self):
        # The pop-up picks quotes and the note by setting, among the sources a setting cites: a
        # passage or note for a setting the entry does not list could never be shown, and two
        # notes of one entry for one setting would both be.
        for e in citations.public()["entries"]:
            listed, noted = set(e["settings"]), []
            for p in e["passages"]:
                self.assertTrue(set(p["settings"]) <= listed, (e["id"], p["key"]))
            for d in e["differs"]:
                self.assertTrue(d["text"] and "—" not in d["text"] and "!" not in d["text"], e["id"])
                self.assertTrue(d["settings"] and set(d["settings"]) <= listed, e["id"])
                noted += d["settings"]
            self.assertEqual(len(noted), len(set(noted)), e["id"])


# ------------------------------------------------------------ previous runs --


def stage_records(decisions):
    out = []
    for key in planning.STAGE_KEYS:
        record = runtime.new_stage(key)
        record.update(status="passed", attempts=1, decisions=list((decisions or {}).get(key) or []))
        out.append(record)
    return out


def add_study(store, sid, codes, reynolds=1.5e6, model=HAIKU, edit=None, title=None):
    study = support.make_study(codes, sid=sid, model=model)
    study["requirements"]["reynolds"] = reynolds
    study["conditions"] = mission.derive_conditions(study["requirements"])
    study["plan"] = support.default_plan(study)[0]
    study.update(status="done", title=title or f"Study {sid}")
    if edit:
        edit(study["plan"])
    store.create_study(study)
    return study


def add_run(store, study, naca, ts, status="done", decisions=None, **row):
    """A finished case of `study` with its row in the runs table."""
    sid, cid = study["id"], f"{study['id']}-{naca}"
    pc = next(c for c in study["plan"]["cases"] if c["naca"] == naca)
    store.create_case({"id": cid, "study_id": sid, "naca": naca, "alpha_deg": float(pc["alpha_deg"]),
                       "dir": f"cases/st_{sid}_{naca}", "status": status, "current_stage": None,
                       "stages": stage_records(decisions), "verdict": row.get("verdict", "pass")})
    store.update_study(sid, lambda s: s.__setitem__("case_ids", s.get("case_ids", []) + [cid]))
    full = {"id": cid, "ts": ts, "project_id": "naca-wing", "study_id": sid, "study_title": study["title"],
            "case_id": cid, "model": study["model"], "naca": naca, "alpha_deg": float(pc["alpha_deg"]),
            "reynolds": study["requirements"]["reynolds"], "verdict": "pass", "success": True, "yplus_avg": 43.84,
            "cells": 23400, "iterations": 1200, "cl_err_pct": -2.661, "cd_err_pct": 14.6, "gate_failures": 0,
            "rework_loops": 0}
    full.update(row)
    store.record_run(full)
    return cid


def engineer_height(plan):
    mesh = plan["cases"][0]["stages"]["mesh"]
    mesh["first_cell_height"] = support.TYPED
    mesh["reasons"]["first_cell_height"] = "Set by the engineer (was 0.00129)."


def sst(plan):
    for pc in plan["cases"]:
        pc["stages"]["physics"]["turbulence_model"] = "kOmegaSST"


def tree(folder):
    """Every file under `folder` with its size and modification time."""
    out = {}
    for base, _, names in os.walk(folder):
        for name in names:
            info = os.stat(os.path.join(base, name))
            out[os.path.join(base, name)] = (info.st_size, info.st_mtime_ns)
    return out


def set_decisions(store, stages):
    """Replace the decisions of the named stages of case hhh-2412: {stage: [(param, value), ...]}."""
    def change(c):
        for s in c["stages"]:
            if s["key"] in stages:
                s["decisions"] = [{"param": p, "value": v, "reason": "r"} for p, v in stages[s["key"]]]
    store.update_case("hhh", "hhh-2412", change)


class PreviousRuns(unittest.TestCase):
    """citations.previous_runs on a store with a history."""

    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)
        self.store = self.eng.store
        self.study = support.store_study(self.eng)              # 'tstudy': NACA 2412 and 4412, Re 1.5e6, no cases yet
        self.planned = self.study["plan"]["cases"][0]["stages"]["mesh"]["first_cell_height"]
        self.names = planning.plan_setting_names()
        # A: the tutorial's run. The engineer typed 0.012, the gate rejected it, the agent used 0.00129.
        a = add_study(self.store, "aaa", ("2412", "4414"), edit=engineer_height, title="Sailplane wing")
        add_run(self.store, a, "2412", "2026-11-18T01:00:00.000Z", gate_failures=1, decisions={
            "mesh": [{"param": "n_normal", "value": 90, "reason": "plan"},
                     {"param": "first_cell_height", "value": 0.00129, "reason": "the gate rejected 0.012"}]})
        add_run(self.store, a, "4414", "2026-11-18T01:00:05.000Z", yplus_avg=54.36)
        # B: another airfoil, later, a model that recorded no decision for the height
        b = add_study(self.store, "bbb", ("4415",), model=SONNET)
        add_run(self.store, b, "4415", "2026-11-18T03:00:00.000Z", verdict="warn", cl_err_pct=7.25, cd_err_pct=31.4)
        # C: the same airfoil at a Reynolds number within a factor of two, scripted
        c = add_study(self.store, "ccc", ("2412",), reynolds=2.5e6, model="scripted")
        add_run(self.store, c, "2412", "2026-11-18T04:00:00.000Z", verdict="fail", success=False, yplus_avg=61.2)
        # not similar, or not finished
        d = add_study(self.store, "ddd", ("2412",), reynolds=4.0e6)
        add_run(self.store, d, "2412", "2026-11-18T05:00:00.000Z")                   # Reynolds too far
        e = add_study(self.store, "eee", ("2412",), edit=sst)
        add_run(self.store, e, "2412", "2026-11-18T05:10:00.000Z")                   # another turbulence model
        f = add_study(self.store, "fff", ("2412",))
        add_run(self.store, f, "2412", "2026-11-18T05:20:00.000Z", status="failed")  # did not finish
        g = add_study(self.store, "ggg", ("2412",))
        add_run(self.store, g, "2412", "2026-11-18T05:30:00.000Z", decisions={      # the agent resolved the wall
            "physics": [{"param": "wall_treatment", "value": "resolved", "reason": "y+ 1"}]})
        self.store.record_run({"id": "gone-2412", "ts": "2026-11-18T06:00:00.000Z", "study_id": "gone",
                               "case_id": "gone-2412", "naca": "2412", "reynolds": 1.5e6, "model": HAIKU})

    def runs(self, setting="mesh.first_cell_height", study="tstudy", case="tstudy-2412"):
        return citations.previous_runs(self.store, setting, study, case, self.names)

    def first(self, setting):
        return self.runs(setting)["runs"][0]

    def test_the_most_similar_runs_come_first(self):
        body = self.runs()
        self.assertEqual((body["setting"], body["value"]), ("mesh.first_cell_height", self.planned))
        self.assertEqual([r["case_id"] for r in body["runs"]], ["aaa-2412", "ccc-2412", "bbb-4415"])
        first = body["runs"][0]
        self.assertEqual(set(first), {"study_id", "study_title", "case_id", "naca", "model", "ts", "reynolds",
                                      "alpha_deg", "value", "same_value", "outcome", "summary"})
        self.assertEqual((first["study_id"], first["study_title"], first["naca"], first["model"]),
                         ("aaa", "Sailplane wing", "2412", HAIKU))
        self.assertEqual((first["reynolds"], first["alpha_deg"], first["ts"]), (1.5e6, 2.5, "2026-11-18T01:00:00.000Z"))
        self.assertEqual(first["outcome"], {"verdict": "pass", "success": True, "yplus_avg": 43.84, "cells": 23400,
                                            "iterations": 1200, "cl_err_pct": -2.661, "cd_err_pct": 14.6,
                                            "gate_failures": 1, "rework_loops": 0})

    def test_the_value_is_the_one_the_run_applied(self):
        first, second, third = self.runs()["runs"]
        # the stage decision, not the engineer's 0.012 that the plan of that study still holds
        self.assertEqual((first["value"], first["same_value"]), (0.00129, True))
        self.assertEqual(first["summary"], "NACA 2412, Re 1.5e6, Haiku 5.5: 0.00129 chord, measured y+ 44, pass")
        # no decision recorded: the plan value, which the higher Reynolds number makes smaller
        plan_c = self.store.get_study("ccc")["plan"]["cases"][0]["stages"]["mesh"]["first_cell_height"]
        self.assertEqual((second["value"], second["same_value"]), (plan_c, False))
        self.assertEqual(second["summary"], f"NACA 2412, Re 2.5e6, Scripted: {plan_c:g} chord, measured y+ 61, fail")
        self.assertEqual((second["outcome"]["verdict"], second["outcome"]["success"]), ("fail", False))
        self.assertEqual(third["summary"], "NACA 4415, Re 1.5e6, Sonnet 5.5: 0.00129 chord, measured y+ 44, warn")

    def test_the_named_case_decides_which_airfoil_is_the_same(self):
        # no earlier run of NACA 4412: the closest Reynolds number first, then the most recent
        expected = ["bbb-4415", "aaa-4414", "aaa-2412"]
        self.assertEqual([r["case_id"] for r in self.runs(case="tstudy-4412")["runs"]], expected)
        self.assertEqual([r["case_id"] for r in self.runs(case="4412")["runs"]], expected)   # the airfoil names it too
        support.make_runner(self.eng, self.store.get_study("tstudy"), naca="4412")          # and a case that exists
        self.assertEqual([r["case_id"] for r in self.runs(case="tstudy-4412")["runs"]], expected)

    def test_other_settings_and_their_summaries(self):
        body = self.runs("mesh.n_normal")
        self.assertEqual((body["value"], body["runs"][0]["value"], body["runs"][0]["same_value"]), (90, 90, True))
        self.assertEqual(body["runs"][0]["summary"], "NACA 2412, Re 1.5e6, Haiku 5.5: n normal 90, 23,400 cells, pass")
        # the count is said once; planned and run are both named only when they differ
        self.assertEqual(self.first("numerics.iterations")["summary"], "NACA 2412, Re 1.5e6, Haiku 5.5: iterations 1200, pass")
        body = self.runs("physics.turbulence_model")
        self.assertEqual(body["value"], "SpalartAllmaras")
        self.assertEqual(body["runs"][2]["summary"], "NACA 4415, Re 1.5e6, Sonnet 5.5: turbulence model "
                                                     "SpalartAllmaras, Cl error 7.2%, Cd error 31%, warn")
        self.assertTrue(all(r["same_value"] for r in body["runs"]))
        self.assertIn(": residual p 1e-6, ran 1200 iterations, ", self.first("numerics.residual_p")["summary"])
        body = self.runs("case.alpha_deg")
        self.assertEqual((body["value"], body["runs"][0]["value"], body["runs"][0]["same_value"]), (2.5, 2.5, True))
        self.assertIn(": 2.5 deg, Cl error 2.7%, Cd error 15%, pass", body["runs"][0]["summary"])
        self.assertEqual([(r["value"], r["same_value"]) for r in self.runs("geometry.naca")["runs"]],
                         [("2412", True), ("2412", True), ("4415", False)])

    def test_a_decision_is_read_as_the_number_it_states(self):
        # '0.00129 chord' was shown as '0.00129 chord chord' and never matched
        h = add_study(self.store, "hhh", ("2412",))
        add_run(self.store, h, "2412", "2026-11-18T07:00:00.000Z", decisions={
            "mesh": [{"param": "first_cell_height", "value": "0.00129 chord", "reason": "as planned"},
                     {"param": "farfield_chords", "value": 30, "reason": "plan"},
                     {"param": "n_normal", "value": "100", "reason": "finer"},
                     {"param": "n_wake", "value": {"cells": 60}, "reason": "odd"}],
            "numerics": [{"param": "relax_U", "value": "0.5 (lowered)", "reason": "odd"},
                         {"param": "relax_p", "value": "lowered to 0.2", "reason": "odd"},
                         {"param": "n_non_orthogonal_correctors", "value": "2 (was 1)", "reason": "odd"}]})
        run = self.runs()["runs"][0]
        self.assertEqual((run["case_id"], run["value"], run["same_value"]), ("hhh-2412", 0.00129, True))
        self.assertEqual(run["summary"], "NACA 2412, Re 1.5e6, Haiku 5.5: 0.00129 chord, measured y+ 44, pass")
        self.assertEqual(self.first("mesh.farfield_chords")["value"], 30.0)
        self.assertEqual((self.first("mesh.n_normal")["value"], self.first("mesh.n_normal")["same_value"]), (100, False))
        # what cannot be read as the number is not shown as a value: the plan's stands
        self.assertEqual(self.first("mesh.n_wake")["value"], 50)
        self.assertEqual(self.first("numerics.relax_p")["value"], 0.3)
        # a remark in parentheses after the number is not part of the value
        self.assertEqual(self.first("numerics.relax_U")["value"], 0.5)
        self.assertEqual(self.first("numerics.n_non_orthogonal_correctors")["value"], 2)
        for text, planned, value in (("2 (was 1)", 1, 2), ("0.5 (lowered after the first gate)", 0.7, 0.5),
                                     ("0.00129 chord (y+ 40)", 0.001, 0.00129), ("30 chords", 30.0, 30.0),
                                     ("23,400 cells in all", 90, 90), ("kept at 0.5, see numerics", 0.7, 0.7),
                                     ("3 x 40", 1, 1), ("30 to 40", 30, 30), ("2 (was 1) then 3", 1, 1),
                                     ("(none)", 1, 1), ("", 1, 1)):
            self.assertEqual(citations._decided(text, planned), value, text)

    def test_the_value_is_the_one_in_force_when_the_case_finished(self):
        # the run monitor may change a numerics setting in the solve stage
        h = add_study(self.store, "hhh", ("2412",))
        add_run(self.store, h, "2412", "2026-11-18T07:00:00.000Z", iterations=1800, decisions={
            "numerics": [{"param": "relax_U", "value": 0.7, "reason": "plan"},
                         {"param": "convection_scheme", "value": "linearUpwind", "reason": "plan"},
                         {"param": "iterations", "value": 1200, "reason": "plan"}],
            "solve": [{"param": "relax_U", "value": 0.5, "reason": "the first run diverged"},
                      {"param": "convection_scheme", "value": "upwind", "reason": "the first run diverged"}],
            "validate": [{"param": "mach", "value": 0.0, "reason": "plan"}]})
        run = self.first("numerics.relax_U")
        self.assertEqual((run["case_id"], run["value"], run["same_value"]), ("hhh-2412", 0.5, False))
        self.assertIn(": relax U 0.5, ran 1800 iterations, pass", run["summary"])
        run = self.first("numerics.convection_scheme")
        self.assertEqual((run["value"], run["same_value"]), ("upwind", False))
        # the solve stage extended the run without a decision of that name: both counts are named
        run = self.first("numerics.iterations")
        self.assertEqual(run["value"], 1200)
        self.assertEqual(run["summary"], "NACA 2412, Re 1.5e6, Haiku 5.5: planned 1200, ran 1800 iterations, pass")
        # a later decision that cannot be read as a value does not hide the readable one of the
        # setting's own stage (the plan's 0.7 was shown as the value in force)
        for remark in ("kept at 0.5, see numerics", "as decided in numerics", "unchanged"):
            set_decisions(self.store, {"numerics": [("relax_U", 0.5)], "solve": [("relax_U", remark)]})
            self.assertEqual(self.first("numerics.relax_U")["value"], 0.5, remark)
        # a readable one of the later stage still wins, also with a remark after the number
        set_decisions(self.store, {"solve": [("relax_U", "0.4 (lowered again)")]})
        self.assertEqual(self.first("numerics.relax_U")["value"], 0.4)
        # the plan value only when no decision can be read
        set_decisions(self.store, {"numerics": [("relax_U", "see the plan")], "solve": [("relax_U", "see the plan")]})
        self.assertEqual(self.first("numerics.relax_U")["value"], 0.7)
        # a decision of an earlier stage is not the setting's, and a later stage that did not pass has no say
        set_decisions(self.store, {"geometry": [("relax_U", 0.1)], "numerics": [("relax_U", 0.7)],
                                   "solve": [("relax_U", 0.5)]})
        self.store.update_case("hhh", "hhh-2412", lambda c: [
            s.update(status="stale") for s in c["stages"] if s["key"] == "solve"])
        self.assertEqual(self.first("numerics.relax_U")["value"], 0.7)

    def test_a_reynolds_number_held_as_text_is_used_as_its_number_or_skipped(self):
        # the guard read the number, the next expression divided the text
        class Rows:
            def __init__(self, store, rows):
                self.store, self.rows = store, rows

            def query_runs(self):
                return self.rows

            def __getattr__(self, name):
                if name == "runs_near":
                    raise AttributeError(name)                           # a store without the bounded query
                return getattr(self.store, name)
        rows = self.store.query_runs()
        for r in rows:
            if r["id"] == "aaa-2412":
                r["reynolds"] = "1.5e6"
            if r["id"] == "bbb-4415":
                r["reynolds"] = "about 1.5 million"
        body = citations.previous_runs(Rows(self.store, rows), "mesh.first_cell_height", "tstudy", "tstudy-2412",
                                       self.names)
        self.assertEqual([(r["case_id"], r["reynolds"]) for r in body["runs"]],
                         [("aaa-2412", 1.5e6), ("ccc-2412", 2.5e6), ("aaa-4414", 1.5e6)])   # bbb-4415 is skipped
        self.assertTrue(body["runs"][0]["summary"].startswith("NACA 2412, Re 1.5e6, Haiku 5.5: "))

    def test_the_work_for_one_pop_up_is_bounded(self):
        # every pop-up read the whole table and copied studies until three matched
        for n in range(12):
            other = add_study(self.store, f"sst{n:02d}", ("2412",), edit=sst)        # never similar
            add_run(self.store, other, "2412", f"2026-11-18T08:00:{n:02d}.000Z")
        with mock.patch.object(citations, "MAX_CANDIDATES", 5), \
                mock.patch.object(self.store, "get_case", wraps=self.store.get_case) as get_case, \
                mock.patch.object(self.store, "query_runs", side_effect=AssertionError("the whole table was read")):
            body = self.runs()
        self.assertLessEqual(get_case.call_count, 5 + 1)                # + the named case itself
        self.assertEqual(body["runs"], [])                             # the five most similar rows are the SST runs
        # with the usual limit the answer is the one of the unbounded search
        self.assertEqual([r["case_id"] for r in self.runs()["runs"]], ["aaa-2412", "ccc-2412", "bbb-4415"])

    def test_runs_near_orders_and_limits_in_the_database(self):
        near = self.store.runs_near(1.5e6, 2.0, not_study="tstudy", naca="2412", limit=200)
        self.assertEqual([r["id"] for r in near][:3], ["gone-2412", "ggg-2412", "fff-2412"])   # same airfoil, same Re, latest
        self.assertNotIn("ddd-2412", [r["id"] for r in near])           # Re 4e6 is outside the factor of two
        self.assertEqual([r["id"] for r in near][-2:], ["bbb-4415", "aaa-4414"])
        self.assertEqual(len(self.store.runs_near(1.5e6, limit=2)), 2)
        self.assertNotIn("aaa-2412", [r["id"] for r in self.store.runs_near(1.5e6, not_study="aaa")])
        self.store.record_run({"id": "text-1", "study_id": "x", "naca": "2412", "reynolds": "abc"})
        self.assertNotIn("text-1", [r["id"] for r in self.store.runs_near(1.5e6)])
        # a value that looks like SQL is data
        self.assertEqual(self.store.runs_near(1.5e6, not_study="x' OR '1'='1", naca="'; DROP TABLE runs; --",
                                              limit=1)[0]["reynolds"], 1.5e6)
        self.assertTrue(self.store.query_runs())

    def test_unknown_things_give_an_empty_list(self):
        empty = {"value": None, "runs": []}
        for setting, study, case in (("mesh.first_cell_height", "nope", "nope-2412"),
                                     ("mesh.first_cell_height", "", ""),
                                     ("mesh.bogus", "tstudy", "tstudy-2412"),
                                     ("first_cell_height", "tstudy", "tstudy-2412"),
                                     ("mesh.cites", "tstudy", "tstudy-2412"),
                                     ("", "tstudy", "tstudy-2412"),
                                     ("mesh.first_cell_height", "tstudy", "tstudy-0012"),
                                     ("mesh.first_cell_height", "tstudy", "")):         # two cases: which one?
            body = citations.previous_runs(self.store, setting, study, case, self.names)
            self.assertEqual({k: body[k] for k in empty}, empty, (setting, study, case))
            self.assertEqual(body["setting"], setting)
        self.assertEqual(citations.previous_runs(self.store, None, None, None)["runs"], [])

    def test_a_study_without_a_plan_or_with_one_case(self):
        add_study(self.store, "one", ("2412",))
        self.assertEqual(len(self.runs(study="one", case="")["runs"]), 3)           # a single case needs no name
        self.assertNotIn("one", [r["study_id"] for r in self.runs(study="one", case="")["runs"]])
        self.store.update_study("one", lambda s: s.__setitem__("plan", None))
        self.assertEqual(self.runs(study="one", case="one-2412"), {"setting": "mesh.first_cell_height",
                                                                   "value": None, "runs": []})

    def test_its_own_study_is_never_a_previous_run(self):
        mine = self.store.get_study("aaa")
        body = self.runs(study="aaa", case="aaa-2412")
        self.assertEqual(body["value"], support.TYPED)                              # the plan's value, as asked
        self.assertNotIn("aaa", [r["study_id"] for r in body["runs"]])
        self.assertEqual(mine, self.store.get_study("aaa"))

    def test_nothing_is_written(self):
        before = tree(self.store.data_dir)
        studies = {sid: self.store.get_study(sid) for sid in ("tstudy", "aaa", "bbb", "ccc")}
        for setting in ("mesh.first_cell_height", "physics.nut_ratio", "case.alpha_deg", "mesh.bogus"):
            self.runs(setting)
        self.assertEqual(tree(self.store.data_dir), before)
        self.assertEqual({sid: self.store.get_study(sid) for sid in studies}, studies)


class RefRoutes(support.ServerCase):
    def setUp(self):
        super().setUp()
        self.folder = use_docs(self, dict(SOUND, **{"stray.md": "not an entry"}), with_differs())

    def test_refs_lists_the_catalogue_and_its_document(self):
        body = self.client.get("/api/refs").json()
        self.assertEqual(body, citations.public())
        self.assertEqual((body["error"], len(body["entries"])), (None, 27))
        e = next(x for x in body["entries"] if x["id"] == "openfoam-linearupwind")
        self.assertEqual((e["doc"], len(e["passages"]), e["differs"]), (True, 2, DIFFERS))
        self.assertEqual(sum(1 for x in body["entries"] if x["doc"]), 1)

    def test_doc_route(self):
        r = self.client.get("/api/refs/doc/openfoam-linearupwind")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), citations.document("openfoam-linearupwind"))
        self.assertEqual(set(r.json()), {"id", "title", "markdown", "source_url", "licence", "retrieved", "scope"})
        self.assertEqual(r.json()["markdown"], MARKDOWN)

    def test_nothing_else_is_served(self):
        with open(os.path.join(os.path.dirname(self.folder), "secret.md"), "w") as f:
            f.write("outside docs")
        for path in ("menter-1994", "stray", "nope", "Openfoam-Linearupwind", "openfoam-linearupwind.md",
                     "..%2Fsecret", "%2E%2E%2Fsecret", "..%2Fcatalogue.json", "openfoam-linearupwind%00",
                     "openfoam-linearupwind/extra", "../secret", ""):
            r = self.client.get("/api/refs/doc/" + path)
            self.assertEqual(r.status_code, 404, path)
            self.assertEqual(list(r.json()), ["error"], path)
            self.assertNotIn("outside docs", r.text)
        self.assertEqual(self.client.get("/api/refs/doc/menter-1994").json(),
                         {"error": "there is no local document for this reference"})
        # no route per id, and neither the catalogue nor the documents are reachable as files
        for path in ("/api/refs/menter-1994", "/files/wingstudio/refs/catalogue.json", "/refs/catalogue.json",
                     "/refs/docs/openfoam-linearupwind.md", "/files/wingstudio/refs/docs/openfoam-linearupwind.md"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/api/refs/runs", json={}).status_code, 405)

    def test_runs_route(self):
        study = support.store_study(self.engine)
        other = add_study(self.store, "aaa", ("2412",), title="Sailplane wing")
        add_run(self.store, other, "2412", "2026-11-18T01:00:00.000Z")
        before = tree(self.data)
        r = self.client.get("/api/refs/runs", params={"setting": "mesh.first_cell_height", "study": study["id"],
                                                      "case": f"{study['id']}-2412"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body, citations.previous_runs(self.store, "mesh.first_cell_height", study["id"],
                                                       f"{study['id']}-2412", planning.plan_setting_names()))
        self.assertEqual((body["value"], len(body["runs"])),
                         (study["plan"]["cases"][0]["stages"]["mesh"]["first_cell_height"], 1))
        for query in ("", "?setting=mesh.first_cell_height", "?setting=mesh.first_cell_height&study=nope&case=x",
                      f"?setting=mesh.bogus&study={study['id']}&case={study['id']}-2412",
                      f"?setting=..%2F..&study={study['id']}&case=..%2F.."):
            r = self.client.get("/api/refs/runs" + query)
            self.assertEqual(r.status_code, 200, query)
            self.assertEqual((r.json()["value"], r.json()["runs"]), (None, []), query)
        self.assertEqual(tree(self.data), before)               # read-only


if __name__ == "__main__":
    unittest.main()
