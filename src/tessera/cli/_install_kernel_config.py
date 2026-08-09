"""Deploy the IPython config that auto-loads the tessera kernel extension.

The ``install-kernel-config`` command writes an ``ipython_config.py`` into an
IPython config directory so notebooks load the tessera kernel client without a
manual ``%load_ext``. It refuses to overwrite an existing file and writes
nothing but the target file.
"""

from __future__ import annotations

from pathlib import Path

from kstlib.ui import PanelManager

from tessera.cli._helpers import _fail

_panels = PanelManager()

# The config body is a plain (not f-string) literal so ``{sys.prefix}`` stays
# verbatim for the reader. ``.append`` is additive: it never clobbers an
# extensions list configured elsewhere.
_CONFIG_BODY = '''"""IPython config auto-loading the tessera kernel extension.

Written by 'tessera install-kernel-config'. Place an IPython config in a
config directory (machine-wide /etc/ipython, or {sys.prefix}/etc/ipython)
so notebooks get TESSERA_TOKEN and get_token without a manual %load_ext.
"""
c = get_config()  # noqa: F821  (injected by the IPython config loader)
c.InteractiveShellApp.extensions.append("tessera.kernel")
'''


def run(*, target: Path) -> None:
    """Write the IPython auto-load config into ``target``, never clobbering.

    The file is created with an exclusive open, so the no-clobber guarantee
    is atomic: there is no window between the existence check and the write.

    Args:
        target: The IPython config directory; the file written is
            ``<target>/ipython_config.py``.
    """
    destination = target / "ipython_config.py"
    try:
        target.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(_CONFIG_BODY)
    except FileExistsError:
        _fail(f"refusing to overwrite {destination}")
    except OSError as exc:
        _fail(f"cannot write to {target} (need root?): {exc}")
    _panels.print_panel(
        "success",
        payload={
            "Written": str(destination),
            "Next": "restart single-user servers / kernels to load it",
        },
    )
