# Wing Studio

LLM agents that set up, run and check 2D OpenFOAM simulations of NACA airfoil
sections. The agents choose the settings, deterministic tools build and run the
cases, and fixed checks decide whether each stage passes.

## How it works

1. You state a mission: a flight condition and an objective, such as the best
   L/D at a design lift.
2. An advisor agent proposes NACA 4-digit sections and screens them in XFoil.
   You pick the ones to simulate.
3. A planner agent writes the settings for every section and stage, with
   reasons and references. You edit and approve the plan.
4. Each section becomes a case. The cases run in parallel, each through seven
   stage agents: geometry, mesh, boundary conditions and physics, solver setup,
   solve, post-process, and validation against XFoil.
5. Each stage ends at a gate. A failed gate goes back to the same agent with
   the reason; after three failures in a row the case waits for you. You can
   message the agents at any point.
6. A reporter agent compares the cases. A dashboard compares models across
   studies, including pass^k reliability.

The web workbench shows the plan, live residuals and forces, a ParaView view of
each case, the source behind each setting and the comparison with XFoil.

## Requirements

- macOS or Linux with Docker ([Colima](https://github.com/abiosoft/colima) works on macOS)
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- An Anthropic API key for the studio's agents (Haiku, Sonnet or Opus)
- Optional: ParaView 6.2, for the viewer pane and the stage images

## Quick start

```bash
uv venv --python 3.12
uv pip install -e .                           # editable: cases/ and studio_data/ live in this checkout
cp .env.example .env                          # add your API key
docker build -t openfoam-xfoil:2512 docker/   # OpenFOAM v2512 and XFoil, once
.venv/bin/wing-studio
```

Open <http://127.0.0.1:8800>. The server has no authentication: it listens on
127.0.0.1 and accepts only local `Host` and `Origin` headers. `STUDIO_PORT`
changes the port; `.env.example` lists the other settings.

The `Scripted` model in the model picker needs no API key. It runs a real study
(OpenFOAM, XFoil, gates) in which every stage applies the plan defaults: a
baseline for comparing models.

## Demo pages

- `/?mock=1` runs the whole interface on recorded data.
- `/?tutorial=1` is a guided tour of a recorded study, about five minutes.
- `/?tutorial=live` is the same tour on a real study with Haiku: about
  8 minutes and 0.10 USD. It needs Docker and the API key, and asks before it
  starts. `/?tutorial=live&model=scripted` runs it with no model calls.

The first two need neither Docker nor an API key.

## ParaView viewer (optional)

Install ParaView 6.2 and give its Python the viewer's packages:

```bash
uv venv --python 3.12 .venv-viewer
uv pip install --python .venv-viewer/bin/python -r wingstudio/viewer/requirements.txt
```

If ParaView is not at `/Applications/ParaView-6.2.1.app`, set `PVPYTHON` in
`.env` to its `pvpython`. Without ParaView the studio runs without the viewer
pane and the stage images.

## Command line

The OpenFOAM tools work without the studio:

```bash
.venv/bin/sim tutorials incompressible cavity                   # JSON tools over OpenFOAM cases
.venv/bin/sim new cavity --from incompressible/icoFoam/cavity/cavity
.venv/bin/of blockMesh -case cases/cavity                       # any OpenFOAM command, in Docker
.venv/bin/python -m wingstudio.cfd.xfoil --naca 4412 --re 2e6 --alpha 4
.venv/bin/foam-agent "Lid-driven cavity at Re=400: where is the vortex centre?"
```

`foam-agent` is a single agent for general OpenFOAM tasks, built on the `sim`
tools. It takes any [LiteLLM](https://docs.litellm.ai/) model (`--model`) and
writes a transcript to `runs/`. [docs/openfoam-tools.md](docs/openfoam-tools.md)
describes `sim` and `of`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m wingstudio.refs.citations --lint    # the reference catalogue
uvx ruff check .                                        # lint
```

The tests need neither Docker nor an API key. A few run the page's JavaScript
with macOS's JavaScriptCore and are skipped on other systems.

## Layout

| Path | Contents |
|---|---|
| `wingstudio/` | the studio: HTTP server, study engine, case pipeline (`runtime.py`), gates, storage |
| `wingstudio/agents/` | agent prompts (`prompts/*.md`, with their tools in the front matter) and the tools |
| `wingstudio/cfd/` | case builder, mesh templates, simpleFoam runs, XFoil driver |
| `wingstudio/study/` | missions, plans, reports, example missions (`presets.json`) |
| `wingstudio/refs/` | reference catalogue and local excerpts of the sources |
| `wingstudio/viewer/` | ParaView viewer and offscreen renders |
| `wingstudio/web/` | web workbench (no build step) and the recorded data of the demo pages |
| `foamtools/` | OpenFOAM in Docker (`of`), the `sim` tools, NACA geometry, `foam-agent` |
| `tests/` | unit tests |
| `docs/` | [OpenFOAM tools](docs/openfoam-tools.md), [reference catalogue](docs/references.md) |
| `docker/` | the OpenFOAM and XFoil image |

`cases/`, `runs/` and `studio_data/` are created at run time and are not tracked.

## Licence

The code is under the [MIT licence](LICENSE). Two directories hold material
under other terms:

- `wingstudio/refs/docs/` holds excerpts of the XFoil primer, the NASA
  Turbulence Modeling Resource, and the OpenFOAM documentation and source code,
  each with its source and the licence its publisher states. They are included
  for non-commercial reference and copyright stays with the publishers. The
  OpenFOAM documentation is CC BY-NC-ND 4.0. See
  [docs/references.md](docs/references.md).
- `wingstudio/web/fonts/` holds IBM Plex, under the SIL Open Font License 1.1
  ([OFL.txt](wingstudio/web/fonts/OFL.txt)).

OpenFOAM is a trademark of OpenCFD Ltd. This project is not approved or endorsed
by OpenCFD Ltd. XFoil is by Mark Drela and Harold Youngren.
