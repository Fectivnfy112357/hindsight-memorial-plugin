"""Pytest config: nothing project-specific needed today.

The package is importable as ``hindsight_memorial`` once pytest's rootdir is
set (see ``pyproject.toml [tool.pytest.ini_options] testpaths``), so no
``sys.path`` injection is required. Kept as a placeholder in case future
fixtures want a ``conftest.py``-scoped setup.
"""