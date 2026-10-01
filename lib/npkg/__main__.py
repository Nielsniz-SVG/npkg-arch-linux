"""Point d'entrée `python3 -m npkg …`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
