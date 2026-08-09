"""JupyterHub configuration for the local tessera bench.

Start the identity provider, then the Hub (from any working directory):

    cd infra && docker compose up -d
    jupyterhub -f infra/jupyterhub_config.py

Everything here is a public test fixture for a loopback bench: the dummy
authenticator accepts any username and password, and the injected values
are deliberately obvious non-secrets matching the imported Keycloak realms.
"""

import os
import sys
from pathlib import Path

c = get_config()  # noqa: F821  (injected by the JupyterHub config loader)

_INFRA = Path(__file__).resolve().parent

# Hub basics: loopback bench with throwaway authentication. The default bind
# port (8000) is kept on purpose: the Keycloak realms register
# http://localhost:8000/services/tessera/callback as the redirect URI.
c.JupyterHub.ip = "127.0.0.1"
c.JupyterHub.authenticator_class = "dummy"
c.JupyterHub.spawner_class = "simple"

# tessera as a Hub-managed service: the Hub spawns `python -m tessera.service`
# and injects the JupyterHub environment contract (service URL, prefix, API
# token). TESSERA_CONFIG is derived from this file's location so the bench
# works from any working directory.
c.JupyterHub.services = [
    {
        "name": "tessera",
        "url": "http://127.0.0.1:10101",
        "command": [sys.executable, "-m", "tessera.service"],
        # Bench-internal service considered part of the Hub: skip the OAuth
        # consent page on first browser access.
        "oauth_no_confirm": True,
        "environment": {
            "TESSERA_CONFIG": str(_INFRA / "servers.yml"),
            # Public test fixtures, matching the declarative realm import.
            "TESSERA_DB_KEY": "test-only-not-a-secret-db-key",
            "TESSERA_CLIENT_SECRET": "test-only-not-a-secret",
            # Passed through so DEBUG can be enabled without editing this file
            # (for example TESSERA_LOG_LEVEL=DEBUG jupyterhub -f ...).
            "TESSERA_LOG_LEVEL": os.environ.get("TESSERA_LOG_LEVEL", "INFO"),
        },
    },
]

# RBAC: the `self` metascope does not include access to services, so without
# this grant every request to /services/tessera would fail with 403.
c.JupyterHub.load_roles = [
    {
        "name": "user",
        "scopes": ["self", "access:services!service=tessera"],
    },
]

# Two different tokens need the service access, governed by two different
# directives (JupyterHub 5 refuses cookie sessions on non-navigation
# requests, so tokens are the only workable credentials):
#
# 1. The PAGE token (PageConfig, the browser's oauth session token) is what
#    the JupyterLab panel sends. Its ADDITIONAL scopes come from
#    oauth_client_allowed_scopes; access to the current server is always
#    included, the default is an empty list.
c.Spawner.oauth_client_allowed_scopes = [
    "access:services!service=tessera",
]

# 2. The ENVIRONMENT token ($JUPYTERHUB_API_TOKEN) is what kernel-side
#    clients send (the Phase 4 API). This directive REPLACES the default
#    `server` role scopes, so they are listed explicitly alongside the
#    service access; the token only ever receives the subset of these
#    scopes held by the user (granted just above).
c.Spawner.server_token_scopes = [
    "users:activity!user",
    "access:servers!server",
    "access:services!service=tessera",
]

# The single-user server environment (distinct from the service environment
# above): the kernel-side client (tessera.kernel) reads TESSERA_URL from here
# to reach the service. This loopback bench serves it at the Hub's default
# bind (port 8000); a real deployment can instead set c.JupyterHub.public_url
# and let the client derive the URL.
c.Spawner.environment = {
    "TESSERA_URL": "http://127.0.0.1:8000/services/tessera/",
}
