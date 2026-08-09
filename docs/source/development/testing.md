# Testing

## TDD, rejection-first

New code lands with its tests written first, and hostile inputs are tested
before the happy path: malformed configuration, oversized values, unknown
or replayed states, forged callbacks, permissive secret files. The coverage
floor is 95% per module, on both stacks, and the Python suite enforces it
(`--cov-fail-under=95`).

Tests exercise the public API and observable behavior only: no test reaches
into protected members, and React components are tested through rendering
and interaction, not internal state.

## Mocked clocks, never sleeps

Everything time-dependent (pending-flow TTL expiry, token expiry,
timestamps) is tested with an injected clock or a patched time source. No
test sleeps: real-sleep tests are structurally flaky and are not accepted.
The one documented exception is the opt-in [end-to-end suite](#end-to-end-suite),
where the identity provider's clock cannot be mocked.

## The three validation tiers

| Tier | What                                            | Where it runs           |
| ---- | ----------------------------------------------- | ----------------------- |
| 1    | Native test suites (pytest, Vitest)             | Any OS, no Hub required |
| 2    | The labextension loaded in a local JupyterLab   | Any OS                  |
| 3    | A real JupyterHub with a test identity provider | Linux host or WSL2      |

Tiers 1 and 2 run everywhere with zero setup. Tier 3 is the
[end-to-end suite](#end-to-end-suite): a real Hub on a Linux host or
WSL2, with the [Keycloak test container](infra/keycloak.md) shipped in
`infra/`.

## Running the suites

```bash
# Python, single environment (fast smoke run with coverage)
tox -e py310

# Python, full matrix (py310 to py314, lint, doctest, docs)
make tox

# TypeScript chain (typecheck, lint, format check, tests, build)
make ts
```

`tox -e py310` runs pytest with branch coverage and fails under the 95%
floor. The OAuth flow tests drive the real kstlib providers over
`httpx.MockTransport`, so the whole Python suite runs with zero network
access.

## End-to-end suite

```bash
pytest -m e2e    # requires the running bench below
```

The end-to-end tier drives the live local bench over pure HTTP: the Hub
login form, the Hub-to-service OAuth handshake, the Keycloak login form,
and the tessera callback, with cookie jars only, no browser, no
JavaScript. It requires the bench to be up:

```bash
cd infra && docker compose up -d
jupyterhub -f infra/jupyterhub_config.py
```

When the bench is down, every test is skipped with that exact
instruction, never failed. The default run excludes the marker, and the
filter applies before node selection: `-m e2e` is required even to run a
single test by node id, otherwise it is silently deselected.

Each run signs in with fresh ephemeral Hub usernames: the token store
starts pristine, manual bench sessions are never touched, and the suite
can be replayed at will. Real sleeps are allowed here and only here: the
bench realms issue 60-second access tokens precisely so expiry, refresh,
and rotation are exercised against the real provider, whose clock cannot
be mocked. Expect roughly 4 to 5 minutes of wall clock.

## Stress and memory

```bash
pytest -m stress    # same live bench as the e2e tier
```

The stress tier proves the service keeps its memory bounded under load.
Like the [end-to-end suite](#end-to-end-suite) it is opt-in, drives the
same live bench, is skipped with the same instruction when the bench is
down, and is allowed the same real sleeps; the default run excludes its
marker as well, so `-m stress` is required even to run a single test by
node id. It runs four load shapes in one process while a background
sampler records the service's resident set size: a sequential regime on
the rotating realm, whose refresh token rotates on every use and so is the
harshest sustained path through the encrypted store; a concurrent burst on
one user and server, exercising the single-flight refresh coalescing under
real concurrency; a flood of logins that are never completed; and a quiet
tail whose growth is checked by a lenient canary that fires only on a
genuine leak. Expect roughly 10 to 15 minutes of wall clock.

The flood targets the one structure that grows with inbound requests, so
its design is worth a note. A pending flow is the state of an
authorization-code exchange in flight, held from the `/login` redirect to
the matching `/callback` and keyed by its anti-CSRF state. The pending-flow
store is bounded two ways: entries older than 600 seconds are evicted on
every add, and a hard cap of 10,000 rejects further adds rather than
growing without limit.

What occupies that store is logins in flight, not signed-in users and not
open connections: an authenticated user holds nothing in it, and a
completed or abandoned flow is gone within the TTL window. Each entry is
about a kilobyte (a username, a provider name, a PKCE verifier, and a
timestamp), so a store filled to the cap holds only a few megabytes of
state. The cap is a guard against a hostile flood, not a capacity limit:
normal operation sits far below it, and a burst of logins that are never
completed, whether accidental or malicious, cannot exhaust memory, because
the store refuses to grow past the cap and the TTL reclaims entries as
they expire.

## Doctests and the docs build

```bash
tox -e doctest   # run the docstring examples as tests
tox -e docs      # build the documentation with warnings as errors
```

The docs build treats every warning as an error (`-W`): a contribution that
adds a page, a cross-reference, or an autodoc entry must build clean.
