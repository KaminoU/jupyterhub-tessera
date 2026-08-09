---
hide-toc: true
---

# tessera Documentation

```{image} ../../assets/tessera_i.svg
:alt: tessera
:width: 240px
:align: center
:class: sd-mb-4
```

JupyterHub/JupyterLab plugin to acquire and securely store OAuth2/OIDC tokens,
with a per-server status button.

tessera adds one button per configured OAuth server inside JupyterLab. On
click, the user authenticates against the provider (authorization-code flow);
a JupyterHub Service receives the callback, exchanges the code, and stores the
refresh token encrypted at rest. The button is green while a valid refresh
token exists, red otherwise. Any OAuth2/OIDC provider can be configured.

```{important}
**tessera is not an Authenticator.** It never signs anyone in to JupyterHub:
your Hub keeps whatever Authenticator it already uses (PAM, LDAP, OAuth, or
any other), and tessera does not replace it. What tessera adds happens after
that login: acquiring tokens from external OAuth2/OIDC providers, where
tessera itself is the registered confidential client, so that notebooks can
call the APIs those providers protect. See {doc}`guide/deployment` for the
deployment sequence.

**tessera requires JupyterHub.** The button is a JupyterLab extension, but
token acquisition, encrypted storage, and user identity all live in a
JupyterHub Service: without a Hub there is nothing for the button to talk
to. A standalone JupyterLab (for example one launched from Anaconda
Navigator) can install the extension, but it stays inactive by design. If a
single-user standalone mode matters to you, please open a GitHub issue so
the demand is visible.
```

```{toctree}
:maxdepth: 2
:caption: Guide

guide/deployment
guide/configuration
guide/notebook
```

```{toctree}
:maxdepth: 2
:caption: API Reference

api/meta
api/config
api/store
api/flow
api/service
api/kernel
api/cli
```

```{toctree}
:maxdepth: 2
:caption: Development

development/architecture
development/environment
development/testing
development/infra/keycloak
```
