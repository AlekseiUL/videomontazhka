#!/usr/bin/env python3
"""Portable filesystem locations for Videomontazhka runtimes and settings.

No directory is created at import time.  Callers may override the application
home with ``VIDEOMONTAZHKA_HOME`` or only the runtime/cache roots with the more
specific variables documented below.  Defaults follow the host platform's
user-data conventions and never depend on the skill's checkout location.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from pathlib import Path


PRODUCT_NAME = "Videomontazhka"
ENV_HOME = "VIDEOMONTAZHKA_HOME"
ENV_RUNTIME_DIR = "VIDEOMONTAZHKA_RUNTIME_DIR"
ENV_CACHE_HOME = "VIDEOMONTAZHKA_CACHE_HOME"
ENV_PYTHON = "VIDEOMONTAZHKA_PYTHON"
ENV_ENV_FILE = "VIDEOMONTAZHKA_ENV_FILE"


def _resolved(value: str | os.PathLike[str], *, cwd: Path | None = None) -> Path:
    """Return a stable absolute path without requiring it to exist."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    return path.resolve(strict=False)


def _absolute_without_dereference(
    value: str | os.PathLike[str], *, cwd: Path | None = None
) -> Path:
    """Make an executable path absolute while preserving virtualenv symlinks."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    return Path(os.path.abspath(path))


def application_home(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Return the writable per-user application-data directory."""

    environment = os.environ if environ is None else environ
    explicit = environment.get(ENV_HOME)
    if explicit:
        return _resolved(explicit, cwd=cwd)

    host = system or platform.system()
    user_home = _resolved(home or Path.home(), cwd=cwd)
    if host == "Darwin":
        return user_home / "Library" / "Application Support" / PRODUCT_NAME
    if host == "Windows":
        base = environment.get("LOCALAPPDATA")
        if base:
            return _resolved(base, cwd=cwd) / PRODUCT_NAME
        return user_home / "AppData" / "Local" / PRODUCT_NAME

    xdg_data = environment.get("XDG_DATA_HOME")
    base = _resolved(xdg_data, cwd=cwd) if xdg_data else user_home / ".local" / "share"
    return base / PRODUCT_NAME.lower()


def runtime_root(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Return the root containing isolated, replaceable tool runtimes."""

    environment = os.environ if environ is None else environ
    explicit = environment.get(ENV_RUNTIME_DIR)
    if explicit:
        return _resolved(explicit, cwd=cwd)
    return application_home(
        environ=environment,
        system=system,
        home=home,
        cwd=cwd,
    ) / "runtime"


def cache_home(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Return the disposable per-user cache directory."""

    environment = os.environ if environ is None else environ
    explicit = environment.get(ENV_CACHE_HOME)
    if explicit:
        return _resolved(explicit, cwd=cwd)

    host = system or platform.system()
    user_home = _resolved(home or Path.home(), cwd=cwd)
    if host == "Darwin":
        return user_home / "Library" / "Caches" / PRODUCT_NAME
    if host == "Windows":
        base = environment.get("LOCALAPPDATA")
        if base:
            return _resolved(base, cwd=cwd) / PRODUCT_NAME / "Cache"
        return user_home / "AppData" / "Local" / PRODUCT_NAME / "Cache"

    xdg_cache = environment.get("XDG_CACHE_HOME")
    base = _resolved(xdg_cache, cwd=cwd) if xdg_cache else user_home / ".cache"
    return base / PRODUCT_NAME.lower()


def component_runtime(
    component: str,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Return one safe direct child of the runtime root."""

    relative = Path(component)
    if not component or relative.is_absolute() or len(relative.parts) != 1 or component in {".", ".."}:
        raise ValueError(f"invalid runtime component: {component!r}")
    return runtime_root(
        environ=environ,
        system=system,
        home=home,
        cwd=cwd,
    ) / component


def venv_executable(runtime: Path, command: str, *, system: str | None = None) -> Path:
    """Return a command path inside a standard virtual environment."""

    host = system or platform.system()
    if host == "Windows":
        suffix = "" if command.lower().endswith((".exe", ".cmd", ".bat")) else ".exe"
        return runtime / "Scripts" / f"{command}{suffix}"
    return runtime / "bin" / command


def configured_python(*, environ: Mapping[str, str] | None = None) -> Path:
    """Prefer the product runtime, then the interpreter running the caller."""

    environment = os.environ if environ is None else environ
    explicit = environment.get(ENV_PYTHON)
    if explicit:
        return _absolute_without_dereference(explicit)
    candidate = venv_executable(component_runtime("python", environ=environment), "python")
    return candidate if candidate.is_file() else _absolute_without_dereference(sys.executable)


def settings_env_file(*, environ: Mapping[str, str] | None = None) -> Path:
    """Return the optional product-local secrets file path."""

    environment = os.environ if environ is None else environ
    explicit = environment.get(ENV_ENV_FILE)
    return _resolved(explicit) if explicit else application_home(environ=environment) / ".env"


APP_HOME = application_home()
RUNTIME_ROOT = runtime_root()
CACHE_HOME = cache_home()
PYTHON_RUNTIME = component_runtime("python")
CREATIVE_PYTHON_RUNTIME = component_runtime("creative-python")
CREATIVE_BROWSER_RUNTIME = component_runtime("creative-browser")
HYPERFRAMES_RUNTIME = component_runtime("hyperframes")
MANIM_RUNTIME = component_runtime("manim")


__all__ = [
    "APP_HOME",
    "CACHE_HOME",
    "CREATIVE_BROWSER_RUNTIME",
    "CREATIVE_PYTHON_RUNTIME",
    "ENV_CACHE_HOME",
    "ENV_ENV_FILE",
    "ENV_HOME",
    "ENV_PYTHON",
    "ENV_RUNTIME_DIR",
    "HYPERFRAMES_RUNTIME",
    "MANIM_RUNTIME",
    "PRODUCT_NAME",
    "PYTHON_RUNTIME",
    "RUNTIME_ROOT",
    "application_home",
    "cache_home",
    "component_runtime",
    "configured_python",
    "runtime_root",
    "settings_env_file",
    "venv_executable",
]
