"""Pytest configuration and environment setup for PyPI SQLite API test suite."""

import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
server_dir = repo_root / "server"
scripts_dir = repo_root / "scripts"

for path in (server_dir, scripts_dir, repo_root):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
