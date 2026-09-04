"""Daemon tests.

When unittest discovery starts at ``test/``, this package can temporarily be
resolved as the top-level ``daemon`` module. Include the production daemon
directory in ``__path__`` so imports such as ``from daemon import server`` keep
resolving to the implementation rather than failing on the test package.
"""
from pathlib import Path

_production_daemon = Path(__file__).resolve().parents[2] / "daemon"
if _production_daemon.is_dir():
    _path = str(_production_daemon)
    if _path not in __path__:
        __path__.append(_path)
