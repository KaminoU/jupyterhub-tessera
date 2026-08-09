# Development environment

## Requirements

- Python 3.10 or newer (the test matrix runs 3.10 through 3.14)
- Node.js 20 or newer

## Python environment

Create the virtual environment **outside any cloud-synced folder** (see the
pitfall below). A conventional layout keeps one environment per project, for
example `C:/dev/uvenv/tessera/py310` on Windows or `~/.venvs/tessera`
elsewhere:

```bash
uv venv C:/dev/uvenv/tessera/py310 --python 3.10
# activate it, then install the project with its dev and tox extras:
uv pip install -e ".[dev,tox]"
```

Any virtual environment manager works; `uv` is what the tox setup uses
under the hood (`tox-uv`).

## Frontend environment

```bash
npm install
```

`node_modules` stays next to `package.json` and is gitignored. The
labextension build is driven by npm scripts; the `make ts` chain expects the
project environment on `PATH` because the build step calls the `jupyter`
command.

## Day-to-day commands

| Command          | What it does                                                        |
| ---------------- | ------------------------------------------------------------------- |
| `make help`      | List the available targets.                                         |
| `make tox`       | Full Python matrix: py310 to py314, lint, doctest, docs.            |
| `make tox-clean` | Clean local caches and recreate the tox environments.               |
| `make ts`        | Full TypeScript chain: typecheck, lint, format check, tests, build. |
| `make green`     | Run both stacks and create the local full-suite marker on success.  |
| `make hook`      | Install the git commit hooks. Run once after cloning.               |

`make green` is the release-gate command: it validates the full dual-stack
suite, and single-environment smoke runs never qualify. The marker it
creates (`.github/.tests-passed`) is local-only and never versioned.

Run `make hook` once after cloning. It copies the `pre-commit` and
`commit-msg` scripts from `scripts/` into `.git/hooks/`, which git never
versions. They refuse a commit that stages a key or a token store, that adds
something shaped like a credential, or that touches functional files without
a `make green` behind it.

## Lint and type checking

- Python: `ruff check`, `ruff format --check`, and `mypy` in strict mode.
  All three run in the `lint` tox environment (`tox -e lint`).
- TypeScript: `tsc --noEmit` (strict), eslint, and prettier. All three run
  at the start of the `make ts` chain.

## Cloud-synced folders

Working on the sources from a cloud-synced folder (OneDrive, Dropbox, and
similar) works, with three rules:

- create the virtual environment outside the synced tree (the tox working
  directory is already placed outside the repository for the same reason);
- keep `node_modules` and build outputs gitignored, as scaffolded;
- pause the synchronization during large build or clean cycles: thousands
  of short-lived files can otherwise trigger sync locks and corrupted
  caches. If an environment ends up broken, `make tox-clean` recreates the
  tox environments from scratch.
