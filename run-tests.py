#!/usr/bin/env python3
"""Lance la suite de tests de npkg sans dépendance (pytest optionnel).

    python3 run-tests.py            # toute la suite
    python3 run-tests.py -k nar     # filtre sur les noms
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT / "lib"))


def main(argv: list[str]) -> int:
    verbosity = 2
    pattern = None
    args = [a for a in argv if a not in ("-v", "-q", "--quiet")]
    for arg in list(args):
        if arg.startswith("-k"):
            args.remove(arg)
            pattern = arg.split("=", 1)[1] if "=" in arg else None
        if arg in ("-q", "--quiet"):
            args.remove(arg)
            verbosity = 1
    if pattern is None and args:
        pattern = args[0] if not args[0].startswith("-") else None
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    if pattern:
        suite = filter_suite(suite, pattern)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    print(
        f"\n{result.testsRun} test(s), {len(result.failures)} échec(s), "
        f"{len(result.errors)} erreur(s), {len(result.skipped)} saut(s)"
    )
    return 0 if result.wasSuccessful() else 1


def filter_suite(suite, needle: str):
    out = unittest.TestSuite()
    for test in iterate(suite):
        if needle.lower() in test.id().lower():
            out.addTest(test)
    return out


def iterate(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iterate(item)
        else:
            yield item


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
