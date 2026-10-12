"""Test bootstrap.

The plugin imports Nastech core (``agent``, ``tools``, ``nastech_cli``) from a checkout: set
``NASTECH_AGENT_REPO`` to your nastech-agent clone (default ``~/.nastech/nastech-agent``), or
``pip install -e`` it. This repo is loaded as the package ``nastech_plugin_holographic`` — the same tree
``nastech plugins install`` copies to ``$NASTECH_HOME/plugins/holographic/``.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

NASTECH_AGENT_REPO = Path(os.environ.get("NASTECH_AGENT_REPO") or Path.home() / ".nastech" / "nastech-agent").expanduser()
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(NASTECH_AGENT_REPO) not in sys.path:
    sys.path.insert(0, str(NASTECH_AGENT_REPO))

PACKAGE = "nastech_plugin_holographic"
if PACKAGE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        PACKAGE, REPO_ROOT / "__init__.py", submodule_search_locations=[str(REPO_ROOT)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[PACKAGE] = _module
    _spec.loader.exec_module(_module)


@pytest.fixture(autouse=True)
def _isolated_nastech_home(tmp_path, monkeypatch):
    """Every test gets its own NASTECH_HOME; nothing touches the real ~/.nastech."""
    home = tmp_path / "nastech_test"
    home.mkdir()
    monkeypatch.setenv("NASTECH_HOME", str(home))
    return home


def _symlinks_supported(tmp_dir: Path) -> bool:
    target, link = tmp_dir / "target", tmp_dir / "link"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return False
    return True


def pytest_configure(config):
    config.addinivalue_line("markers", "require_symlinks: skip when symbolic links cannot be created here")


def pytest_runtest_setup(item):
    if item.get_closest_marker("require_symlinks"):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            if not _symlinks_supported(Path(tmp)):
                pytest.skip("symbolic links are not supported here (Windows needs developer mode)")
