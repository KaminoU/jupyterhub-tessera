<p align="center">
  <img src="https://raw.githubusercontent.com/KaminoU/jupyterhub-tessera/main/assets/tessera_i.svg" alt="tessera logo" width="240">
</p>

<p align="center">
  <strong>Acquire and securely store OAuth2/OIDC tokens in JupyterHub.</strong>
</p>

<p align="center">
  <a href="https://github.com/KaminoU/jupyterhub-tessera/actions/workflows/ci.yml"><img src="https://github.com/KaminoU/jupyterhub-tessera/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://jupyterhub-tessera.readthedocs.io/"><img src="https://img.shields.io/badge/docs-RTD-blue" alt="Documentation"></a>
  <a href="https://pypi.org/project/jupyterhub-tessera/"><img src="https://img.shields.io/pypi/v/jupyterhub-tessera?color=blue" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-≥3.10-blue" alt="Python">
  <a href="https://github.com/KaminoU/jupyterhub-tessera/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

---

tessera adds a per-server button in JupyterLab. On click, the user runs an
OAuth2/OIDC authorization-code flow (confidential client, PKCE). A JupyterHub
Service receives the callback, exchanges the code, and stores the refresh token
encrypted at rest. Notebooks then read a fresh access token from the Service on
demand. The button is green when a valid refresh token exists, red otherwise.
tessera is generic: any OAuth2/OIDC provider is a configuration entry.

> **tessera is not an Authenticator.** It never signs anyone in to JupyterHub:
> your Hub keeps whatever Authenticator it already uses (PAM, LDAP, OAuth, or
> any other). tessera runs a separate step after that login, acquiring tokens
> from external OAuth2/OIDC providers so notebooks can call the APIs those
> providers protect.

<p align="center">
  <img src="https://raw.githubusercontent.com/KaminoU/jupyterhub-tessera/main/assets/poc-viya.png" alt="The tessera panel in JupyterLab: one button per declared server, green when a valid refresh token is stored, with token details on demand" width="720">
</p>

<p align="center">
  <em>Signed in on 6 August, still green on 10 August: the access token expired
  several times in between, and each one was renewed from the stored refresh
  token, on demand.</em>
</p>

## How it works

1. **One button per declared server.** The panel shows a button for each OAuth
   server in your configuration, green when a valid refresh token is stored for
   that user, red otherwise.
2. **A click starts an authorization-code flow with PKCE.** The Service
   generates the `state` and the PKCE verifier, sends the browser to the
   provider with the S256 challenge, and keeps the verifier to itself.
3. **The provider calls back to the Service**, which validates the `state`
   against the flow it started and exchanges the code for tokens. The browser
   never handles a token.
4. **The refresh token is encrypted at rest**, with the encryption key held
   separately from the database file, so the database on its own reveals
   nothing.
5. **Notebooks read an access token on demand.** The kernel client
   (`get_token("my-idp")`, or `TESSERA_TOKEN["my-idp"]`) fetches a fresh token
   from the Service on each access. Nothing is cached kernel-side and no token
   ever sits in an environment variable, so a token revoked at the provider is
   never served from a stale copy.

## Architecture (three surfaces)

| Surface   | Stack            | Role                                                                                            |
| --------- | ---------------- | ----------------------------------------------------------------------------------------------- |
| Frontend  | TypeScript/React | Per-server status button (green/red), shipped as a prebuilt JupyterLab extension.               |
| Service   | Python 3.10+     | JupyterHub Service exposing `/login`, `/callback`, `/token`; encrypted token store.             |
| Admin CLI | Python (Typer)   | Deployment and ops helpers: `config-snippet`, `init-config`, `doctor`, `install-kernel-config`. |

## Install

```bash
pip install jupyterhub-tessera
```

Installing the Python package also ships the prebuilt JupyterLab extension; no
manual frontend copy is required.

## Project layout

| Path            | What lives there                                                     |
| --------------- | -------------------------------------------------------------------- |
| `src/tessera/`  | Service, admin CLI, kernel client, and the prebuilt lab extension    |
| `frontend/src/` | TypeScript/React panel sources, with their tests alongside them      |
| `tests/`        | Python test suite (config, flow, store, service, kernel, end to end) |
| `docs/`         | Sphinx documentation sources                                         |

## Development

Requirements: Python 3.10+, Node.js 20+.

```bash
# Python (use a virtual environment created outside any synced folder)
pip install -e ".[dev]"

# Frontend
npm install
npm run build
```

## Security

A refresh token and a client secret are encrypted at rest, with the encryption
key held separately from the database. The frontend never reads, stores, or
logs a token; it only consumes a boolean status.

## License

[MIT](LICENSE), Copyright (c) 2026 Michel TRUONG.
