# Reference catalogue

`wingstudio/refs/catalogue.json` lists every source the studio may cite. A plan
setting or a stage decision carries `cites`, a list of ids from this file. Agents
never write titles, authors or addresses: everything the UI shows about a source
comes from the catalogue.

- `wingstudio/refs/citations.py` loads it, checks the ids a model writes, and
  serves it as `GET /api/refs` (one local document as `GET /api/refs/doc/<id>`).
- The planner sees the whole list; each stage agent sees the entries for its
  stage. Agents are shown `id`, `kind`, `title`, `says` and the entry's settings
  (as `cite_for`), never an address.
- The web page builds the citation pop-up (`wingstudio/web/js/cite.js`) and the
  reference viewer (`wingstudio/web/ref.html`, `wingstudio/web/js/ref.js`) from it.

## Shape

```
{ "version": 1, "checked": "YYYY-MM-DD",
  "entries":  [ { id, title, kind, source, locator, says, settings, differs?, url, url_verified, checked } ],
  "defaults": { "<stage>.<setting>": [id, ...] } }
```

| Field | Rule |
|---|---|
| `id` | `[a-z0-9]+(-[a-z0-9]+)*`, at most 64 characters, unique, never changed (stored studies point at it) |
| `kind` | `paper`, `book`, `documentation`, `project-measurement` or `project-rule` |
| `title`, `source` | as published; for a project entry, the files and sections that hold the evidence |
| `locator` | where in the source the statement is; say so when it was not checked |
| `says` | one or two sentences in our own words (see below) |
| `settings` | the `stage.setting` names the entry supports; decides which agents see it |
| `differs` | optional `[ { settings, text } ]`: where the studio departs from the source |
| `url`, `url_verified` | an `https://` address, verified only when it was opened and shows the source |
| `checked` | the date the address or the named files were last confirmed |

`defaults` lists up to three ids per plan setting. A value left at its default is
cited with them (`wingstudio/study/planning.py`, `section_cites`). Settings that
no source fixes are left out.

## Rules

- `says` states what the source establishes, in our own words, and never more than
  the source does. Numbers in a project entry must be findable in the named files.
- A `differs` note is one or two sentences: what the source says, then what the
  studio does instead. At most one note per setting per entry.
- A removed id is shown as "no longer in the catalogue"; prefer correcting an entry
  in place to deleting it, and never reuse an id.
- When a default value changes, review its `defaults` list and `differs` notes.

## Local documents

Eleven `documentation` entries have a local excerpt in `wingstudio/refs/docs/`:

```
<id>.md     a header block (Source, Retrieved, Publisher, Licence, Scope), then the text
<id>.json   { id, title, source_url, retrieved, licence, scope,
              passages [ { key, heading, text, settings, why } ] }
```

Each passage `text` is an exact substring of the `.md` file and is quoted word for
word from the source. A passage lists only the settings it says something about,
and the most direct statement comes first. Excerpts change layout only, never
wording; omissions are marked `[...]`.

| Source | Documents | Licence as found |
|---|---|---|
| XFOIL primer, `web.mit.edu` | `xfoil-primer` | none stated in the file; XFOIL itself is GPL |
| NASA Turbulence Modeling Resource | `nasa-tmr-spalart-allmaras`, `nasa-tmr-sst`, `nasa-tmr-naca0012` | none stated on the pages |
| OpenFOAM documentation v2606 | `openfoam-linearupwind`, `openfoam-simple-relaxation`, `openfoam-simple-algorithm`, `openfoam-wall-functions`, `openfoam-freestream-bc`, `openfoam-naca0012-validation` | CC BY-NC-ND 4.0, copyright OpenCFD Ltd |
| OpenFOAM v2512 source | `openfoam-checkmesh-thresholds` | GPL v3 or later |

The excerpts are included verbatim, with attribution, for non-commercial
reference. Copyright stays with each publisher, and an excerpt will be removed on
request.

## Adding an entry

1. Add the entry at the end of its group (the order is the order of the reference
   page) and fill every field by the rules above.
2. Add it to `defaults` only when it supports the default value.
3. For a local document, write `<id>.md` and `<id>.json` as above.
4. Check:

   ```bash
   .venv/bin/python -m wingstudio.refs.citations --lint
   .venv/bin/python -m unittest discover -s tests
   ```
