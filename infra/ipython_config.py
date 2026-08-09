"""IPython config auto-loading the tessera kernel extension.

Place this in an IPython config directory, or run
``tessera install-kernel-config``:

- machine-wide (admin): /etc/ipython/ipython_config.py
- per environment: {sys.prefix}/etc/ipython/ipython_config.py

The extension only needs ``tessera`` installed in the kernel environment
and the JupyterHub-injected environment (``$JUPYTERHUB_API_TOKEN`` and
``TESSERA_URL``).
"""

c = get_config()  # noqa: F821  (injected by the IPython config loader)

c.InteractiveShellApp.extensions.append("tessera.kernel")
