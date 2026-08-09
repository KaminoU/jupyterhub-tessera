"""Project-level Sphinx configuration for the tessera documentation site."""

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import inspect
import sys
from pathlib import Path
from typing import Any

_TYPER_PARAM_TYPES: tuple[type[Any], ...] = ()
try:  # Typer is optional during doc builds.
    from typer.models import ArgumentInfo as _ArgumentInfo
    from typer.models import OptionInfo as _OptionInfo
except Exception:  # noqa: BLE001
    pass
else:
    _TYPER_PARAM_TYPES = (_ArgumentInfo, _OptionInfo)

# -- Path setup --------------------------------------------------------------
# Add the project sources to sys.path for autodoc.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from tessera.meta import (  # noqa: E402
    __app_name__,
    __author__,
    __version__,
)

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = __app_name__
copyright = f"2026, {__author__}"  # noqa: A001  # Sphinx requires this name.
author = __author__
release = __version__
version = __version__

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinx.ext.autodoc",  # Auto-generate docs from docstrings.
    "sphinx.ext.napoleon",  # Support for Google style docstrings.
    "sphinx.ext.viewcode",  # Add [source] links to documentation.
    "sphinx.ext.intersphinx",  # Link to other projects' documentation.
    "sphinx.ext.todo",  # Support for todo items.
    "sphinx.ext.coverage",  # Check documentation coverage.
    "sphinx.ext.autosummary",  # Generate autodoc summaries.
    "myst_parser",  # Markdown authoring via MyST.
    "sphinx_togglebutton",  # Collapsible sections (dropdown directive).
    "sphinx_design",  # Design components (cards, tabs, dropdowns).
]

# MyST configuration so Markdown and reST can be mixed.
myst_enable_extensions = [
    "colon_fence",
    "deflist",
]
myst_heading_anchors = 3

# Napoleon settings (for Google style docstrings).
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = False
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = True
napoleon_type_aliases = None
napoleon_attr_annotations = True

# Autodoc settings.
autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "special-members": "__init__",
    "undoc-members": True,
    "exclude-members": "__weakref__",
}
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"

# Autosummary settings.
autosummary_generate = True

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
exclude_patterns: list[str] = []

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = "sphinx"

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "furo"
html_title = __app_name__
html_logo = "../../assets/tessera_h.svg"

html_theme_options = {
    # The logo already carries the name: no text under it in the sidebar.
    "sidebar_hide_name": True,
    # GitHub integration: adds "Edit on GitHub" and source links.
    "source_repository": "https://github.com/KaminoU/jupyterhub-tessera",
    "source_branch": "main",
    "source_directory": "docs/source/",
    # Footer icons with GitHub link.
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/KaminoU/jupyterhub-tessera",
            "html": """
                <svg stroke="currentColor" fill="currentColor" stroke-width="0" viewBox="0 0 16 16">
                    <path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path>
                </svg>
            """,
            "class": "",
        },
    ],
}


def _sanitize_parameter_default(default: Any) -> Any:
    """Return a human-friendly default for Typer parameters when possible."""
    if _TYPER_PARAM_TYPES and isinstance(default, _TYPER_PARAM_TYPES):
        normalized = getattr(default, "default", inspect.Signature.empty)
        if normalized in (inspect.Signature.empty, Ellipsis):
            return inspect.Signature.empty
        return normalized
    return default


def _unwrap_signature_target(obj: Any) -> Any:
    """Best-effort inspect.unwrap() that tolerates non-callables."""
    try:
        return inspect.unwrap(obj)
    except Exception:  # noqa: BLE001
        return obj


def _normalized_signature(obj: Any) -> inspect.Signature | None:
    """Return the callable signature when introspection succeeds."""
    target = _unwrap_signature_target(obj)
    try:
        return inspect.signature(target)
    except (TypeError, ValueError):
        return None


def _autodoc_clean_signature(  # noqa: PLR0913
    app: Any,
    what: str,
    name: str,
    obj: Any,
    options: Any,
    signature: str | None,
    return_annotation: str | None,
) -> tuple[str, Any] | None:
    """Replace noisy signatures for decorated functions before rendering."""
    del app, name, options, signature  # Unused autodoc parameters.
    if what not in {"function", "method"}:
        return None

    normalized = _normalized_signature(obj)
    if normalized is None:
        return None

    cleaned_parameters = []
    for param in normalized.parameters.values():
        sanitized_default = _sanitize_parameter_default(param.default)
        if sanitized_default is not param.default:
            param = param.replace(default=sanitized_default)
        cleaned_parameters.append(param)

    sanitized_signature = normalized.replace(parameters=cleaned_parameters)
    rendered_signature = str(sanitized_signature)
    rendered_return: str | None
    if sanitized_signature.return_annotation is not inspect.Signature.empty:
        rendered_return = str(sanitized_signature.return_annotation)
    else:
        rendered_return = return_annotation

    return (rendered_signature, rendered_return)


def setup(app: Any) -> None:
    """Register custom hooks for the documentation build.

    Args:
        app: Active Sphinx application instance.
    """
    app.connect("autodoc-process-signature", _autodoc_clean_signature)


# -- Options for intersphinx extension ---------------------------------------
# https://www.sphinx-doc.org/en/master/usage/extensions/intersphinx.html#configuration

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- Options for todo extension ----------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/extensions/todo.html#configuration

todo_include_todos = True
