# Contributing to tessera

Thanks for considering a contribution. This page covers what tessera
structurally is (which decides what a contribution can and cannot change), who
owns what in a deployment, and the quality bar a change has to clear. The
setup and testing details live in the development documentation, linked below
rather than repeated here.

## tessera is a JupyterHub Service

The visible part is a JupyterLab panel, but the component that does the work is
a Hub-managed service. That is not an implementation preference: an OAuth
client secret and a refresh token can never live in a browser, so a trusted
server-side component is structural to the product.

Three consequences for contributions:

- Moving a secret or a token toward the browser (local storage, a cookie
  readable from JavaScript, a token in page state, a token in a console log) is
  outside the contract, not a trade off to weigh. The frontend consumes a
  boolean status and nothing else.
- Running tessera without a Hub is not a feature waiting to be written: user
  identity, the OAuth callback, and the encrypted store all live in the
  service. If a standalone JupyterLab mode matters to you, open a GitHub issue
  so the demand is visible.
- Anything that widens what the service returns to the browser deserves an
  issue before a pull request. The service exposes status and actions; it does
  not hand tokens to the page.

## RBAC belongs to the Hub administrator

tessera never grants itself scopes. Three distinct credentials reach the
service, and each one needs its own grant in `jupyterhub_config.py`. That is
deployment configuration, documented once in the deployment guide: see the
section "Understand the three scopes you just granted" in
[docs/source/guide/deployment.md](docs/source/guide/deployment.md).

The practical consequence while developing: a `403` from your local Hub is
almost always a missing grant in your Hub configuration rather than a defect in
the service. Compare the three grants before opening an issue.

## Setting up

tessera is dual stack: a Python service (3.10 or newer) and a TypeScript/React
frontend, both driven from the repository root.

| You want to                   | Read                                                                               |
| ----------------------------- | ---------------------------------------------------------------------------------- |
| Set up either stack           | [docs/source/development/environment.md](docs/source/development/environment.md)   |
| Run the suites, including e2e | [docs/source/development/testing.md](docs/source/development/testing.md)           |
| Understand how it fits        | [docs/source/development/architecture.md](docs/source/development/architecture.md) |
| Deploy it for real            | [docs/source/guide/deployment.md](docs/source/guide/deployment.md)                 |

## The quality bar

Write the test first. A change arrives with its tests in the same pull request,
and rejection paths count: what has to be refused matters as much as what has
to work.

| Stack      | Bar                                                                           |
| ---------- | ----------------------------------------------------------------------------- |
| Python     | coverage 95% or more per module, `mypy --strict` clean, `ruff` check + format |
| TypeScript | coverage 95% or more per module, `tsc --strict` clean, ESLint and Prettier    |

One command runs the whole thing:

```console
$ make green
```

It runs the full Python matrix (py310 to py314, plus lint, doctest and the
documentation build), then the complete TypeScript chain (typecheck, lint,
format check, tests with coverage, production build). It is the gate: a run in
a single environment is a smoke test, not a validation.

While iterating you can run one side at a time with `make tox` or `make ts`,
and `make tox-clean` recreates the tox environments when a cache goes bad. On
success, `make green` creates `.github/.tests-passed`, a local marker that is
never committed.

## Token hygiene in a contribution

tessera handles OAuth refresh and access tokens, so contributions carry an
extra rule: never commit a token, a client secret, an encryption key, or a
token store database, test fixtures included. Use obviously fabricated values
in tests. In code you add, never log a token value, not even at DEBUG level:
log a length, a redaction, or nothing at all.

## Opening a pull request

- One subject per pull request.
- Describe the behavior that changes, not the diff.
- Say which stack you touched, and whether the other one needs a follow up.
- If you notice something broken outside your scope, mention it in the pull
  request instead of fixing it silently in the same change.

Contributions are accepted under the [MIT license](LICENSE), the license this
project ships under.
