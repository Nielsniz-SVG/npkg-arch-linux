"""Rend le paquet importable dans les tests sans installation."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "lib"))
