# Dual-stack developer commands for tessera (Python service + TypeScript frontend).

.DEFAULT_GOAL := help

.PHONY: help tox tox-clean ts green hook

help:
	@echo "Development targets:"
	@echo "  tox        -> Run the full Python matrix (py310-py314, lint, doctest, docs)"
	@echo "  tox-clean  -> Clean local caches and recreate the tox environments"
	@echo "  ts         -> Run the full TypeScript chain (typecheck, lint, format, tests, build)"
	@echo "  green      -> Run tox + ts, then create the local full-suite marker on success"
	@echo "  hook       -> Install the git hooks (run once after cloning)"
	@echo ""
	@echo "Note: 'green' is the release-gate command. It validates the FULL dual-stack"
	@echo "suite; single-env smoke runs never qualify. The marker it creates"
	@echo "(.github/.tests-passed) is local-only and never versioned."
	@echo ""
	@echo "The 'ts' chain needs the project dev environment on PATH (the labextension"
	@echo "build step calls the 'jupyter' command)."

# Full Python matrix. Does NOT touch the fast-commit marker: only 'green'
# (both stacks passing) is allowed to create it.
tox:
	@tox

# Recreate the tox environments after cleaning local caches (fixes corrupted caches).
tox-clean:
	@python -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache', '.mypy_cache', '.ruff_cache', 'htmlcov']]; pathlib.Path('.coverage').unlink(missing_ok=True)"
	@tox -r

# Full TypeScript chain: typecheck, lint, format check, tests with coverage, build.
ts:
	@npx tsc --noEmit
	@npm run eslint:check
	@npm run prettier:check
	@npm run test:coverage
	@npm run build:prod

# Full dual-stack gate: create the fast-commit marker only when BOTH stacks pass.
# The .github directory may not exist in a fresh checkout: create it first.
green: tox ts
	@python -c "from pathlib import Path; Path('.github').mkdir(exist_ok=True); Path('.github/.tests-passed').touch(); print('\033[32m[OK] Full dual-stack suite passed. Marker created.\033[0m')"

# Install the commit gates locally. Run once after cloning: git does not
# version .git/hooks, so a shim is written there that runs the tracked script.
# A shim rather than a copy, so editing scripts/ takes effect at the next
# commit instead of waiting for someone to remember to reinstall.
#
# The shim runs `sh script` rather than the script itself: the tracked files
# carry no exec bit, so a fresh clone on Linux could not execute them directly.
# Git runs hooks from the top of the working tree, which is also what the
# scripts assume for their own relative paths.
#
# Written in python (cross-platform): make runs recipes without a POSIX shell
# on Windows, where cp and chmod do not exist. The quotes and the dollar of
# "$@" are emitted through chr() so the recipe carries no shell quoting and no
# dollar at all: make would otherwise expand $@ into the target name, and the
# commit message would silently reach the hook as no argument.
hook:
	@python -c "from pathlib import Path; import os, stat; [[Path('.git/hooks/' + h).write_text('#!/bin/sh\nexec sh scripts/' + h + ' ' + chr(34) + chr(36) + '@' + chr(34) + '\n', newline='\n'), os.chmod('.git/hooks/' + h, os.stat('.git/hooks/' + h).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)] for h in ('pre-commit', 'commit-msg')]; print('Installed pre-commit and commit-msg into .git/hooks/')"
