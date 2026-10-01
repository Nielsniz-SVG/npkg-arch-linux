"""Analyse de lempage Hydra et index nixpkgs local (fixtures réelles)."""

import unittest
from pathlib import Path

from npkg.hydra import jobset_for_channel, parse_build_page
from npkg.index import Entry, Index

FIXTURES = Path(__file__).parent / "fixtures"


class TestHydra(unittest.TestCase):
    def setUp(self):
        page = FIXTURES / "hydra_build_hello.html"
        if not page.exists():
            self.skipTest("fixture hydra_build_hello.html absente")
        self.build = parse_build_page(page.read_text(), attr="hello", jobset="unstable")

    def test_champs_attendus(self):
        self.assertEqual(self.build.nixname, "hello-2.12.3")
        self.assertEqual(self.build.version, "2.12.3")
        self.assertEqual(
            self.build.description, "Program that produces a familiar, friendly greeting"
        )
        self.assertEqual(self.build.license, "gpl3Plus")
        self.assertEqual(self.build.homepage, "https://www.gnu.org/software/hello/manual/")
        self.assertEqual(self.build.system, "x86_64-linux")

    def test_sorties_et_derivation(self):
        self.assertEqual(
            self.build.outputs,
            {"out": "/nix/store/5z2yp3ysx8476c8g5w25b0smlgkjvaq3-hello-2.12.3"},
        )
        self.assertEqual(self.build.out_path, "/nix/store/5z2yp3ysx8476c8g5w25b0smlgkjvaq3-hello-2.12.3")
        self.assertTrue(self.build.drv_path.endswith(".drv"))
        self.assertNotIn("-source", list(self.build.outputs.values()))

    def test_rev_et_taille(self):
        self.assertEqual(self.build.rev, "6c6973cdf55fbe86579846439ec5ddd9a1198e46")
        self.assertIn("MiB", self.build.closure_size)
        self.assertTrue(self.build.available)
        self.assertEqual(self.build.build_id, 347137367)

    def test_jobsets_par_canal(self):
        self.assertEqual(jobset_for_channel("nixpkgs-unstable"), ["unstable"])
        self.assertEqual(
            jobset_for_channel("nixpkgs-25.05"), ["staging-25.05", "release-2505", "unstable"]
        )
        self.assertIn("unstable", jobset_for_channel("inconnu"))


class TestIndex(unittest.TestCase):
    def setUp(self):
        import tempfile

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_import_nixpkgs_et_recherche(self):
        root = self.tmp / "nixpkgs"
        (root / "pkgs" / "tools" / "mpv").mkdir(parents=True)
        (root / "pkgs" / "tools" / "mpv" / "default.nix").write_text(
            '''{ lib, stdenv }:
stdenv.mkDerivation rec {
  pname = "mpv";
  version = "0.40.0";

  src = fetchurl { url = "https://example.org/mpv.tar.gz"; };

  meta = {
    description = "a free and open source media player";
    homepage = "https://mpv.io/";
    license = licenses.lgpl21Plus;
    mainProgram = "mpv";
  };
}
'''
        )
        (root / "pkgs" / "by-name" / "he" / "hello").mkdir(parents=True)
        (root / "pkgs" / "by-name" / "he" / "hello" / "package.nix").write_text(
            '''{ lib }:
{
  pname = "hello";
  version = "2.12.3";
  meta.description = "Program that produces a familiar, friendly greeting";
  meta.license = lib.licenses.gpl3Plus;
}
'''
        )
        index = Index(self.tmp / "index.json")
        added = index.import_nixpkgs(root)
        self.assertEqual(added, 2)
        index.save()
        reloaded = Index(self.tmp / "index.json")
        self.assertEqual(reloaded.stats()["packages"], 2)
        entry = reloaded.get("hello")
        self.assertEqual(entry.version, "2.12.3")
        self.assertEqual(entry.license, "gpl3Plus")
        mpv = reloaded.get("mpv")
        self.assertIn("media player", mpv.description)
        self.assertEqual(mpv.main_program, "mpv")

        hits = reloaded.search("mpv")
        self.assertEqual(hits[0][1].attr, "mpv")
        self.assertGreater(hits[0][0], 0)
        self.assertEqual([entry.attr for _, entry in reloaded.search("media player")], ["mpv"])
        self.assertEqual(reloaded.search("zzz-inexistant"), [])

    def test_tris_par_score(self):
        index = Index(None)
        index.add(Entry(attr="python313Packages.requests", pname="requests"))
        index.add(Entry(attr="requests", pname="requests"))
        hits = index.search("requests")
        self.assertEqual(hits[0][1].attr, "requests")
        self.assertGreater(hits[0][0], hits[1][0])


if __name__ == "__main__":
    unittest.main()
