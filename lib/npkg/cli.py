"""Interface en ligne de commande de npkg."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import index as index_mod
from . import nar as nar_mod
from . import zstd
from .cache import substituters as default_substituters
from .hashing import sha256_bytes
from .http import FetchError, NotFound, get_text, join_url
from .ops import Engine, Options, human_size
from .state import LockBusy, locked
from .store import parse_store_path

EXIT_OK, EXIT_USAGE, EXIT_ERROR = 0, 2, 1


# ---------------------------------------------------------------------------
# affichage
# ---------------------------------------------------------------------------

class Printer:
    def __init__(self, *, color: bool | None = None, json_mode: bool = False, quiet: bool = False):
        self.json_mode = json_mode
        self.quiet = quiet
        if color is None:
            color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.color = color
        self.bold = "\033[1m" if color else ""
        self.dim = "\033[2m" if color else ""
        self.green = "\033[32m" if color else ""
        self.yellow = "\033[33m" if color else ""
        self.red = "\033[31m" if color else ""
        self.reset = "\033[0m" if color else ""

    def out(self, text: str = "") -> None:
        if not self.json_mode and not self.quiet:
            print(text)

    def line(self, text: str = "") -> None:
        if self.quiet:
            return
        print(text, file=sys.stderr if self.json_mode else sys.stdout)

    def ok(self, text: str) -> None:
        self.out(f"{self.green}✓{self.reset} {text}")

    def warn(self, text: str) -> None:
        if text:
            self.out(f"{self.yellow}!{self.reset} {text}")

    def fail(self, text: str) -> None:
        print(f"{self.red}✗{self.reset} {text}", file=sys.stderr)

    def title(self, text: str) -> None:
        self.out(f"{self.bold}{text}{self.reset}")

    def data(self, payload: Any) -> None:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))

    def kv(self, key: str, value: Any, *, width: int = 14) -> None:
        if value in (None, "", [], {}):
            return
        self.out(f"  {key.ljust(width)} {value}")


def _rows(table: list[list[Any]], headers: list[str], printer: Printer) -> str:
    rows = [[str(cell) for cell in row] for row in table]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    if printer.color:
        lines[0] = f"{printer.bold}{lines[0]}{printer.reset}"
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def _age(timestamp: float | None) -> str:
    if not timestamp:
        return "jamais"
    delta = max(0.0, time.time() - timestamp)
    for unit, size in (("j", 86400), ("h", 3600), ("min", 60)):
        if delta >= size:
            return f"{int(delta // size)}{unit}"
    return f"{int(delta)}s"


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

COMMANDS = (
    "install remove uninstall reinstall search info list closure tree size which env "
    "history rollback status doctor init gc verify index update pin unpin pins nar completion"
)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--version", default=argparse.SUPPRESS, action="version", version=f"npkg {__version__}")
    common.add_argument("--home", default=argparse.SUPPRESS, help="répertoire d'état ($NPKG_HOME, défaut ~/.local/state/npkg)")
    common.add_argument("--store-dir", default=argparse.SUPPRESS, help="répertoire du store (défaut /nix/store si écrivable)")
    common.add_argument("--channel", default=argparse.SUPPRESS, help="canal nixpkgs ($NPKG_CHANNEL, défaut nixpkgs-unstable)")
    common.add_argument("--system", default=argparse.SUPPRESS, help="système cible (défaut : détecté)")
    common.add_argument("--jobset", default=argparse.SUPPRESS, help="jobset Hydra forcé (unstable, staging-26.05, …)")
    common.add_argument("--cache", action="append", default=argparse.SUPPRESS, help="substituer (répétable) ; remplace la liste par défaut")
    common.add_argument("--jobs", "-j", type=int, default=argparse.SUPPRESS, help="téléchargements parallèles (défaut 4)")
    common.add_argument("--no-verify", action="store_true", default=argparse.SUPPRESS, help="ne pas vérifier FileHash/NarHash (déconseillé)")
    common.add_argument("--dry-run", "-n", action="store_true", default=argparse.SUPPRESS, help="planifie sans rien écrire")
    common.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS, help="ignore les caches (Hydra, narinfo)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="sortie machine")
    common.add_argument("--quiet", "-q", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--no-nix", action="store_true", default=argparse.SUPPRESS, help="ignorer nix même s'il est installé")
    parser = argparse.ArgumentParser(
        prog="npkg",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Gestionnaire de paquets Linux minimaliste adossé aux canaux nixpkgs.",
        epilog=(
            "exemples\n"
            "  npkg install hello                    installe hello du canal nixpkgs-unstable\n"
            "  npkg install -n ripgrep               montre le plan (fermeture, tailles) sans rien faire\n"
            "  npkg info nix                         métadonnées + chemin du store résolu\n"
            "  npkg tree hello --depth 3             graphe de dépendances réel\n"
            "  npkg channel nixpkgs-stable           change de canal pour les résolutions\n"
            "  npkg rollback                         annule la dernière mutation du profil\n"
            "  npkg gc && npkg verify                ménage + contrôle d'intégrité\n"
            "\n"
            "aucune installation de Nix n'est nécessaire : npkg résout via Hydra et\n"
            "dépaquette lui-même les NAR de cache.nixos.org (avec nix, c'est encore mieux).\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<commande>")

    def add(name: str, help_text: str, *, aliases: tuple[str, ...] = ()):
        return sub.add_parser(
            name,
            help=help_text,
            description=help_text,
            aliases=list(aliases),
            parents=[common],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    p = add("install", "installe des paquets depuis nixpkgs")
    p.add_argument("specs", nargs="+", help="nom, attr.path, nom@version ou chemin du store")
    p.add_argument("--comment", help="commentaire stocké avec la racine")
    p.set_defaults(func=cmd_install)

    p = add("remove", "désinstalle des racines", aliases=("uninstall",))
    p.add_argument("names", nargs="+")
    p.add_argument("--gc", action="store_true", help="ramasse les chemins devenus orphelins")
    p.set_defaults(func=cmd_remove)

    p = add("reinstall", "vérifie et refait les chemins abîmés")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_reinstall)

    p = add("search", "cherche dans l'index nixpkgs")
    p.add_argument("query", nargs="?")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_search)

    p = add("info", "métadonnées et chemin résolu d'un paquet")
    p.add_argument("spec")
    p.set_defaults(func=cmd_info)

    p = add("list", "paquets installés (racines)", aliases=("ls",))
    p.add_argument("--long", "-l", action="store_true")
    p.set_defaults(func=cmd_list)

    p = add("closure", "tous les chemins du store nécessaires à un paquet")
    p.add_argument("spec")
    p.add_argument("--sizes", action="store_true")
    p.set_defaults(func=cmd_closure)

    p = add("tree", "arborescence des dépendances")
    p.add_argument("spec")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--reverse", action="store_true", help="qui référence ce paquet")
    p.set_defaults(func=cmd_tree)

    p = add("size", "poids de la fermeture")
    p.add_argument("spec")
    p.set_defaults(func=cmd_size)

    p = add("which", "chemin réel d'une commande installée")
    p.add_argument("program")
    p.set_defaults(func=cmd_which)

    p = add("env", "ligne PATH à évaluer")
    p.add_argument("--shell", choices=("sh", "fish", "csh"), default="sh")
    p.add_argument("--profile", default="default")
    p.add_argument("--after", action="store_true",
                   help="placer le profil en fin de PATH (ne masque ls/cat/grep du système)")
    p.set_defaults(func=cmd_env)

    p = add("history", "générations du profil")
    p.add_argument("--profile", default="default")
    p.set_defaults(func=cmd_history)

    p = add("rollback", "revient à la génération précédente du profil")
    p.add_argument("--to", type=int, help="génération cible (défaut : la précédente)")
    p.add_argument("--profile", default="default")
    p.set_defaults(func=cmd_rollback)

    p = add("status", "état de l'installation")
    p.set_defaults(func=cmd_status)

    p = add("doctor", "diagnostic d'environnement")
    p.set_defaults(func=cmd_doctor)

    p = add("init", "prépare /nix/store (sudo) pour exécuter les binaires nixpkgs")
    p.add_argument("--store", default="/nix/store", help="répertoire de store à créer (défaut /nix/store)")
    p.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_init)

    p = add("channel", "affiche ou change le canal nixpkgs par défaut")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_channel)

    p = add("gc", "libère les chemins du store non référencés")
    p.add_argument("--clear-cache", action="store_true", help="vide aussi les caches narinfo/Hydra/tmp")
    p.set_defaults(func=cmd_gc)

    p = add("verify", "contrôle présence, dépendances et NarHash")
    p.add_argument("names", nargs="*")
    p.add_argument("--shallow", action="store_true", help="ne pas recalculer les hachages NAR")
    p.set_defaults(func=cmd_verify)

    p = add("index", "gère l'index de recherche")
    p.add_argument(
        "action", nargs="?", default="stats",
        choices=("fetch", "import", "stats", "clear", "show"),
    )
    p.add_argument("path", nargs="?", help="checkout nixpkgs pour `import`")
    p.add_argument("--ref", default=index_mod.DEFAULT_REF, help="référence git pour by-name")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_index)

    p = add("update", "rafraîchit l'index de recherche")
    p.add_argument("--ref", default=index_mod.DEFAULT_REF)
    p.set_defaults(func=cmd_update)

    p = add("pin", "épingle un attribut sur un chemin du store")
    p.add_argument("attr")
    p.add_argument("store_path")
    p.set_defaults(func=cmd_pin)

    p = add("unpin", "retire une épingle")
    p.add_argument("attr")
    p.set_defaults(func=cmd_unpin)

    p = add("pins", "liste les épingles")
    p.set_defaults(func=cmd_pins)

    p = add("nar", "outils NAR bruts (miroirs, debug)")
    p.add_argument("action", choices=("ls", "dump", "hash"))
    p.add_argument("path")
    p.set_defaults(func=cmd_nar)

    p = add("completion", "script d'autocomplétion bash/zsh")
    p.add_argument("shell", choices=("bash", "zsh"), nargs="?", default="bash")
    p.set_defaults(func=cmd_completion)
    return parser


def make_engine(args, printer: Printer) -> Engine:
    options = Options.from_env(
        home=Path(getattr(args, "home", None)).expanduser() if getattr(args, "home", None) else None,
        store_dir=Path(getattr(args, "store_dir", None)).expanduser() if getattr(args, "store_dir", None) else None,
        channel=getattr(args, "channel", None),
        system=getattr(args, "system", None),
        jobset=getattr(args, "jobset", None),
        caches=getattr(args, "cache", None) or None,
        workers=getattr(args, "jobs", None),
        verify=not getattr(args, "no_verify", False),
        dry_run=getattr(args, "dry_run", False),
        use_nix=False if getattr(args, "no_nix", None) else None,
        quiet=getattr(args, "quiet", None) or printer.quiet,
    )
    if not getattr(args, "channel", None) and not os.environ.get("NPKG_CHANNEL"):
        options.channel = saved_channel(options.home) or "nixpkgs-unstable"
    return Engine(options, log=printer.line)


def saved_channel(home: Path | None) -> str:
    """Le canal mémorisé dans state.json prime sur le défaut, mais pas sur --channel."""
    try:
        blob = json.loads((Path(home) / "state.json").read_text())
        return blob.get("channel") or ""
    except (OSError, json.JSONDecodeError):
        return ""


def index_of(engine: Engine) -> index_mod.Index:
    return index_mod.Index(engine.home.cache_dir / "index.json")


# ---------------------------------------------------------------------------
# commandes
# ---------------------------------------------------------------------------

def cmd_install(args, engine: Engine, printer: Printer) -> int:
    try:
        report = engine.install(args.specs, comment=args.comment or "", refresh=getattr(args, "refresh", None))
    except (LookupError, FetchError, ValueError) as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    if getattr(args, "json", False):
        printer.data(
            {
                "installed": report.installed,
                "dry_run": engine.opts.dry_run,
                "fetched": len(report.fetched),
                "bytes_downloaded": report.bytes_downloaded,
                "closure_bytes": report.bytes_on_disk,
                "generation": report.generation,
                "notes": report.notes,
                "note_count": len(report.notes),
                "resolutions": [res.as_dict() for res in report.resolutions],
            }
        )
        return EXIT_OK
    printer.title(
        f"npkg install — canal {engine.opts.channel}, {engine.opts.system}, "
        f"store {engine.store.root}"
    )
    for res in report.resolutions:
        printer.out(
            f"  {res.name:<22} {(res.version or '-'):<14} {Path(res.store_path).name}"
        )
        printer.out(
            f"  {'':<22} {'':<14} {printer.dim}résolu via {res.source}"
            + (f" ({res.jobset})" if res.jobset else "")
            + f"{printer.reset}"
        )
    visible = report.notes
    for note in visible[:8]:
        printer.warn(note)
    if len(visible) > 8:
        printer.warn(
            f"… et {len(visible) - 8} autre(s) — `npkg install --json` liste tout, "
            "`npkg remove <paquet>` lève la collision"
        )
    if engine.opts.dry_run:
        printer.out(f"\n  fermeture : {len(report.fetched)} chemin(s) à télécharger, "
                    f"{human_size(report.bytes_on_disk)} au total")
        for path in report.fetched:
            printer.out(f"    - {Path(path).name}")
        printer.out("  (mode --dry-run : rien n'a été écrit)")
        return EXIT_OK
    printer.ok(
        f"{len(report.installed)} paquet(s) installés — {len(report.fetched)} chemin(s) "
        f"dépaquetés, {human_size(report.bytes_downloaded)} reçus en {report.seconds:.1f}s"
    )
    if report.generation:
        printer.out(f"  profil : génération {report.generation} -> {engine.home.profile()}")
    printer.out(f"  {printer.bold}eval $(npkg env){printer.reset} pour ajouter le profil au PATH")
    demo = next((name for name in report.installed if engine.which(name)), None)
    if demo:
        printer.out(f"  ensuite : {printer.bold}{demo}{printer.reset}")
    return EXIT_OK


def cmd_remove(args, engine: Engine, printer: Printer) -> int:
    try:
        report = engine.uninstall(args.names)
    except LookupError as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    printer.ok(f"désinstallé : {', '.join(report.removed)} — profil génération {report.generation}")
    for note in report.notes:
        printer.warn(note)
    if args.gc:
        gc_report = engine.gc()
        printer.out(f"  gc : {gc_report.notes[0] if gc_report.notes else 'rien'}, "
                    f"{human_size(gc_report.freed)} libérés")
    return EXIT_OK


def cmd_reinstall(args, engine: Engine, printer: Printer) -> int:
    roots = engine.state.roots
    unknown = [name for name in args.names if name not in roots]
    if unknown:
        printer.fail(f"pas des racines npkg : {', '.join(unknown)}")
        return EXIT_ERROR

    # Vérifie que chaque paquet est toujours résolu vers le chemin
    # actuellement utilisé par npkg. Cela détecte notamment un changement
    # de store (/ancien/store -> /nix/store).
    moved = []
    for name in args.names:
        current = roots[name]
        resolution = engine.resolve_specs([name])[0]
        if current.store_path != resolution.store_path:
            moved.append((name, current.store_path, resolution.store_path))

    if moved:
        for name, old_path, new_path in moved:
            printer.line(
                f"  {name} : store changé "
                f"{Path(old_path).parent} -> {Path(new_path).parent}"
            )
            engine.invalidate(old_path)

    results = engine.verify(
        [name for name in args.names if name not in {item[0] for item in moved}],
        deep=True,
    ) if len(moved) < len(args.names) else []

    broken = [item for item in results if not item["ok"]]

    if not moved and not broken:
        printer.ok("rien à refaire : " + ", ".join(args.names))
        return EXIT_OK

    for item in broken:
        printer.line(f"  {Path(item['path']).name} : " + "; ".join(item["checks"]))
        engine.invalidate(item["path"])

    report = engine.install(args.names)
    printer.ok(
        f"refait : {', '.join(report.installed)} "
        f"({len(report.fetched)} chemin(s) téléchargés)"
    )
    return EXIT_OK


def cmd_search(args, engine: Engine, printer: Printer) -> int:
    index = index_of(engine)
    stats = index.stats()
    if not index.entries:
        printer.warn("index vide : `npkg index fetch` (réseau) ou `npkg index import <nixpkgs>`")
        return EXIT_OK
    results = index.search(args.query or "", limit=args.limit)
    if getattr(args, "json", False):
        printer.data([entry.as_dict() | {"score": score} for score, entry in results])
        return EXIT_OK
    if not results:
        printer.fail(f"rien dans l'index ({stats['packages']} paquets) pour {args.query!r}")
        printer.out(f"  `npkg info {args.query}` interroge Hydra directement")
        return EXIT_ERROR
    table = [
        [entry.attr, entry.version or "-", (entry.description or "—")[:68]]
        for _, entry in results
    ]
    printer.out(_rows(table, ["attribut", "version", "description"], printer))
    printer.out(f"\n  {len(results)} résultat(s) sur {stats['packages']} indexés "
                f"({stats['with_description']} avec description)")
    return EXIT_OK


def cmd_info(args, engine: Engine, printer: Printer) -> int:
    try:
        res = engine.resolver.resolve(args.spec, refresh=getattr(args, "refresh", None))
    except (LookupError, FetchError) as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    if getattr(args, "json", False):
        printer.data(res.as_dict())
        return EXIT_OK
    printer.title(f"{res.name} {res.version}".strip())
    printer.kv("attribut", res.attr)
    printer.kv("description", res.description)
    printer.kv("licence", res.license)
    printer.kv("page", res.homepage)
    printer.kv("sortie", res.store_path)
    printer.kv("sorties", ", ".join(sorted(res.outputs)) if len(res.outputs) > 1 else "")
    printer.kv("résolution", res.source + (f" via {res.jobset}" if res.jobset else ""))
    if res.build_date:
        age = res.build_age_days
        printer.kv("build hydra", f"{res.build_date}" + (f" ({age:.0f} j)" if age is not None else ""))
        if age is not None and age > 120:
            printer.warn(
                f"build très ancien (≈{age / 365:.1f} an(s)) : l'attribut a peut-être été "
                "renommé ; comparez avec `npkg search`"
            )
    printer.kv("nixpkgs", res.rev)
    printer.kv("dérivation", res.drv_path)
    entry = engine.valid.get(res.store_path)
    printer.kv("store", "présent" if entry and Path(res.store_path).exists() else "absent")
    installed = [r for r in engine.state.roots.values() if r.store_path == res.store_path]
    printer.kv("racine", installed[0].name if installed else "non installée")
    if res.closure_size:
        printer.kv("fermeture", res.closure_size + " (annonce Hydra)")
    return EXIT_OK


def cmd_list(args, engine: Engine, printer: Printer) -> int:
    roots = engine.state.roots
    if getattr(args, "json", False):
        printer.data({name: root.as_dict() for name, root in roots.items()})
        return EXIT_OK
    if not roots:
        printer.warn("aucun paquet installé (`npkg install hello`)")
        return EXIT_OK
    table = []
    for root in roots.values():
        size = sum(
            (engine.valid.get(p).nar_size or 0) for p in root.closure if engine.valid.get(p)
        )
        row = [root.name, root.version or "-", len(root.closure), human_size(size)]
        if args.long:
            row += [time.strftime("%Y-%m-%d %H:%M", time.localtime(root.installed)), root.attr]
        table.append(row)
    headers = ["paquet", "version", "chemins", "fermeture"] + (["installé", "attribut"] if args.long else [])
    printer.out(_rows(table, headers, printer))
    printer.out(f"\n  {len(roots)} racine(s) — {len(engine.store.list_paths())} chemins dans {engine.store.root}")
    return EXIT_OK


def cmd_closure(args, engine: Engine, printer: Printer) -> int:
    try:
        store_path, nodes = engine.closure_of(args.spec)
    except (LookupError, FetchError) as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    total = sum(node.size for node in nodes.values())
    if getattr(args, "json", False):
        printer.data(
            {
                "root": store_path,
                "bytes": total,
                "paths": {n.store_path: n.size for n in sorted(nodes.values(), key=lambda x: -x.size)},
            }
        )
        return EXIT_OK
    table = []
    for node in sorted(nodes.values(), key=lambda n: -n.size):
        present = Path(node.store_path).exists() or engine.valid.get(node.store_path) is not None
        table.append([node.name, human_size(node.size), "présent" if present else "à télécharger"])
    printer.out(_rows(table, ["paquet", "NarSize", "état"], printer))
    printer.out(f"\n  {len(nodes)} chemin(s), {human_size(total)} — racine {Path(store_path).name}")
    return EXIT_OK


def cmd_tree(args, engine: Engine, printer: Printer) -> int:
    try:
        store_path, nodes = engine.closure_of(args.spec)
    except (LookupError, FetchError) as exc:
        printer.fail(str(exc))
        return EXIT_ERROR

    def name_of(path: str) -> str:
        node = nodes.get(path)
        return node.name if node else Path(path).name

    def walk(path: str, depth: int, seen: set) -> None:
        node = nodes.get(path)
        if node is None:
            return
        if args.reverse:
            children = sorted(p for p, other in nodes.items() if path in other.children and p != path)
        else:
            children = [c for c in node.children if c != path]
        for child in children:
            repeat = child in seen
            mark = "├─" if not repeat else "└─"
            printer.out(
                "  " + "   " * depth + f"{mark} {name_of(child)}"
                + (f" {printer.dim}(déjà vu){printer.reset}" if repeat else "")
            )
            if not repeat and depth + 1 < args.depth:
                walk(child, depth + 1, seen | {child})

    printer.title(name_of(store_path) + ("  (ce qui en dépend)" if args.reverse else ""))
    walk(store_path, 0, {store_path})
    printer.out(f"\n  fermeture complète : {len(nodes)} chemin(s) ; profondeur affichée {args.depth}")
    return EXIT_OK


def cmd_size(args, engine: Engine, printer: Printer) -> int:
    try:
        total = engine.size_of(args.spec)
    except (LookupError, FetchError) as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    if getattr(args, "json", False):
        printer.data({"spec": args.spec, "nar_bytes": total})
    else:
        printer.out(f"{args.spec} : {human_size(total)} de NAR ({total} octets)")
    return EXIT_OK


def cmd_which(args, engine: Engine, printer: Printer) -> int:
    found = engine.which(args.program)
    if found is None:
        printer.fail(f"{args.program} n'est dans aucun profil npkg")
        return EXIT_ERROR
    target = Path(os.path.realpath(str(found)))
    if getattr(args, "json", False):
        printer.data({"program": args.program, "link": str(found), "target": str(target)})
        return EXIT_OK
    printer.out(str(found))
    printer.out(f"  -> {target}")
    try:
        printer.out(f"  -> paquet {target.relative_to(engine.store.root).parts[0]}")
    except (ValueError, IndexError):
        pass
    return EXIT_OK


def cmd_env(args, engine: Engine, printer: Printer) -> int:
    print(engine.profiles.env_script(args.profile, args.shell, after=args.after))
    return EXIT_OK


def cmd_history(args, engine: Engine, printer: Printer) -> int:
    gens = engine.profiles.generations(args.profile)
    if getattr(args, "json", False):
        printer.data(gens)
        return EXIT_OK
    if not gens:
        printer.warn("aucune génération (`npkg install` en crée une)")
        return EXIT_OK
    table = [
        [
            f"{item['generation']}",
            "*" if item["current"] else "",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["created"])),
            len(item["roots"]),
            ", ".join(sorted(Path(p).name for p in item["roots"])[:2]) or "-",
        ]
        for item in reversed(gens)
    ]
    printer.out(_rows(table, ["gén.", "act.", "date", "racines", "contenu"], printer))
    return EXIT_OK


def cmd_rollback(args, engine: Engine, printer: Printer) -> int:
    gens = engine.profiles.generations(args.profile)
    current = engine.profiles.current_generation(args.profile)
    if args.to is not None:
        target = args.to
    else:
        previous = [g["generation"] for g in gens if g["generation"] != current]
        if not previous:
            printer.fail("aucune génération antérieure")
            return EXIT_ERROR
        target = max(previous)
    try:
        engine.profiles.switch(args.profile, target)
    except FileNotFoundError as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    roots = next((item["roots"] for item in gens if item["generation"] == target), [])
    printer.ok(f"profil {args.profile} -> génération {target} ({len(roots)} paquet(s))")
    if roots:
        printer.out("  " + ", ".join(sorted(Path(p).name for p in roots)[:6]))
    missing = [p for p in roots if not Path(p).exists()]
    if missing:
        printer.warn(
            f"{len(missing)} chemin(s) de cette génération ont été collectés depuis : "
            + ", ".join(Path(p).name for p in missing[:3])
            + (" …" if len(missing) > 3 else "")
            + " — `npkg install <paquet>` pour les refaire"
        )
    printer.warn(
        "le profil seul change : `npkg list` (les racines) garde la trace de l'installation courante"
    )
    return EXIT_OK


def cmd_status(args, engine: Engine, printer: Printer) -> int:
    status = engine.status()
    if getattr(args, "json", False):
        printer.data(status)
        return EXIT_OK
    printer.title("npkg status")
    printer.kv("état", status["home"])
    printer.kv("store", f"{status['store']} ({'écrivable' if status['store_writable'] else 'non écrivable'})")
    printer.kv("paquets", f"{len(status['roots'])} racine(s) : {', '.join(status['roots']) or '-'}")
    printer.kv("store", f"{status['store_paths']} chemins, {human_size(status['store_bytes'])}")
    printer.kv("canal", f"{status['channel']} ({status['system']})")
    printer.kv("profil", f"{status['profile']} — génération {status['generation']}")
    printer.kv("résolution", ", ".join(status["backends"]))
    printer.kv("miroirs", status["substituters"])
    printer.kv("zstd", status["zstd"])
    if not status["nix_store_layout"]:
        printer.warn(
            f"store = {status['store']} et non /nix/store : les binaires dont\n"
            "     l'interpréteur est /nix/store/… ne s'exécuteront pas → `npkg init`"
        )
    return EXIT_OK


def cmd_channel(args, engine: Engine, printer: Printer) -> int:
    if not args.name:
        printer.out(engine.state.channel)
        return EXIT_OK
    with locked(engine.home):
        engine.state.channel = args.name
        engine.state.system = engine.opts.system or engine.state.system
        engine.state.save()
    printer.ok(f"canal -> {args.name} (utilisé par `npkg install`, `npkg info`)")
    return EXIT_OK


def cmd_doctor(args, engine: Engine, printer: Printer) -> int:
    printer.title("npkg doctor")
    problems = 0

    def check(label: str, ok: bool, detail: str = "", hint: str = "") -> None:
        nonlocal problems
        mark = f"{printer.green}✓{printer.reset}" if ok else f"{printer.red}✗{printer.reset}"
        printer.out(f"  {mark} {label:<22} {detail}")
        if not ok:
            problems += 1
            if hint:
                printer.out(f"      {printer.yellow}→ {hint}{printer.reset}")

    check("python", sys.version_info >= (3, 9), sys.version.split()[0], "Python >= 3.9 requis")
    backend = zstd.zstd_backend()
    check("zstd", backend != "none", backend,
          "`pip install zstandard`, paquet `zstd`, ou Python >= 3.14")
    root = engine.store.root
    check("store écrivable", os.access(root, os.W_OK), str(root), "`npkg init` ou NPKG_STORE_DIR=…")
    check("layout /nix/store", str(root) == "/nix/store", str(root),
          "hors /nix/store, les binaires nixpkgs ne démarrent pas (interpréteur en dur)")
    paths = engine.store.list_paths()
    tracked = [p for p in paths if engine.valid.get(p) is not None]
    check("chemins suivis", len(tracked) == len(paths), f"{len(tracked)}/{len(paths)}", "`npkg gc`")
    nix = shutil.which("nix")
    check("nix (facultatif)", True, nix or "absent — npkg résout via Hydra")
    try:
        free = shutil.disk_usage(str(root))[2]
        check("espace disque", free > 200 * 1024 * 1024, f"{human_size(free)} libres sur {root}")
    except OSError:
        pass
    if not engine.opts.quiet:
        for cache in (engine.opts.caches or default_substituters()):
            short = cache.split("//")[-1].rstrip("/")[:20]
            try:
                text = get_text(join_url(cache, "nix-cache-info"), timeout=8, retries=1)
                fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
                check(f"miroir {short}", True,
                      f"StoreDir={fields.get('StoreDir', '?').strip()} prio={fields.get('Priority', '?').strip()}")
            except (FetchError, NotFound) as exc:
                check(f"miroir {short}", False, str(exc)[:56],
                      "réseau ? ou `--cache URL` / NPKG_SUBSTITUTERS")
    index = index_of(engine)
    stats = index.stats()
    check("index recherche", bool(index.entries), f"{stats['packages']} paquets", "`npkg index fetch`")
    check("verrou npkg", lock_free(engine), "", "un autre npkg travaille peut-être")
    bindir = engine.home.profile() / "bin"
    in_path = str(bindir) in os.environ.get("PATH", "").split(":")
    check("profil dans PATH", in_path or not bindir.exists(), str(bindir), "`eval $(npkg env)`")
    printer.out()
    if problems:
        printer.warn(f"{problems} point(s) à corriger")
        return EXIT_ERROR
    printer.ok("nickel")
    return EXIT_OK


def lock_free(engine: Engine) -> bool:
    lock = engine.home.root / "lock"
    if not lock.exists():
        return True
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def cmd_gc(args, engine: Engine, printer: Printer) -> int:
    report = engine.gc(dry_run=getattr(args, "dry_run", False))
    if getattr(args, "json", False):
        printer.data({"dry_run": args.dry_run, "notes": report.notes, "freed": report.freed})
        return EXIT_OK
    if args.dry_run:
        for path in report.notes[1:]:
            printer.out(f"  {path}")
        printer.out(f"\n  {report.notes[0] if report.notes else 'rien à libérer'}"
                    + (f", {human_size(report.freed)}" if report.notes else ""))
    else:
        printer.ok(f"{report.notes[0] if report.notes else '0 chemin(s) supprimés'}, "
                   f"{human_size(report.freed)} récupérés")
    if args.clear_cache:
        shutil.rmtree(engine.home.narinfo_dir, ignore_errors=True)
        engine.home.narinfo_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(engine.home.cache_dir / "hydra", ignore_errors=True)
        shutil.rmtree(engine.home.tmp_dir, ignore_errors=True)
        engine.home.tmp_dir.mkdir(parents=True, exist_ok=True)
        printer.out("  caches narinfo/hydra/tmp vidés")
    return EXIT_OK


def cmd_verify(args, engine: Engine, printer: Printer) -> int:
    try:
        results = engine.verify(args.names or None, deep=not args.shallow)
    except LookupError as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    if getattr(args, "json", False):
        printer.data(results)
        return EXIT_OK
    if not results:
        printer.warn("rien d'installé à vérifier")
        return EXIT_OK
    broken = [item for item in results if not item["ok"]]
    for item in results:
        mark = f"{printer.green}✓{printer.reset}" if item["ok"] else f"{printer.red}✗{printer.reset}"
        printer.out(f"  {mark} {Path(item['path']).name:<44} {'; '.join(item['checks'])}")
    if broken:
        printer.fail(f"{len(broken)} paquet(s) en défaut sur {len(results)} — `npkg reinstall <nom>`")
        return EXIT_ERROR
    printer.ok(f"{len(results)} paquet(s) vérifiés : contenu, dépendances et NarHash bons")
    return EXIT_OK


def cmd_index(args, engine: Engine, printer: Printer) -> int:
    action = args.action or "stats"
    index = index_of(engine)
    if action == "stats":
        if getattr(args, "json", False):
            printer.data(index.stats())
            return EXIT_OK
        stats = index.stats()
        printer.title("index npkg")
        printer.kv("paquets", stats["packages"])
        printer.kv("documentés", stats["with_description"])
        printer.kv("réf by-name", stats["by_name_ref"])
        printer.kv("importé de", stats["imported_from"])
        printer.kv("âgé de", _age(stats["updated"]))
        where = Path(stats["file"]) if stats["file"] else None
        if where and where.exists():
            printer.kv("fichier", f"{where} ({human_size(where.stat().st_size)})")
        return EXIT_OK
    if action == "show":
        printer.data(
            index.stats()
            | {"sample": [index.entries[k].as_dict() for k in sorted(index.entries)[:30]]}
        )
        return EXIT_OK
    if action == "clear":
        if index.path:
            index.path.unlink(missing_ok=True)
        printer.ok("index supprimé")
        return EXIT_OK
    if action == "import":
        if not args.path:
            printer.fail("usage : npkg index import <répertoire-nixpkgs>")
            return EXIT_USAGE
        t0 = time.time()
        added = index.import_nixpkgs(Path(args.path).expanduser(), limit=args.limit)
        index.save()
        printer.ok(f"{added} paquets indexés depuis {args.path} en {time.time() - t0:.1f}s")
        return EXIT_OK
    return _index_fetch(index, args.ref, engine, printer)


def cmd_update(args, engine: Engine, printer: Printer) -> int:
    return _index_fetch(index_of(engine), args.ref, engine, printer)


def _index_fetch(index: index_mod.Index, ref: str, engine: Engine, printer: Printer) -> int:
    engine.home.cache_dir.mkdir(parents=True, exist_ok=True)
    printer.line(f"  énumération de nixpkgs/pkgs/by-name (réf {ref}) …")
    t0 = time.time()
    added = index.fetch_by_name(
        ref=ref, progress=lambda letter, info: printer.line(f"    {letter}  {info}")
    )
    index.save()
    if not added:
        printer.warn("aucun nom récupéré (API GitHub injoignable ou limitée ?)")
        return EXIT_ERROR
    stats = index.stats()
    printer.ok(
        f"{added} nom(s) ajoutés ({stats['packages']} au total) en {time.time() - t0:.1f}s"
    )
    if not stats["with_description"]:
        printer.out("  index léger (noms) : `npkg index import <nixpkgs>` ajoute les descriptions")
    return EXIT_OK


def cmd_pin(args, engine: Engine, printer: Printer) -> int:
    try:
        parse_store_path(args.store_path)
    except ValueError as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    engine.resolver.add_pin(args.attr, args.store_path)
    printer.ok(f"épinglé {args.attr} -> {args.store_path}")
    return EXIT_OK


def cmd_unpin(args, engine: Engine, printer: Printer) -> int:
    if engine.resolver.remove_pin(args.attr):
        printer.ok(f"épingle de {args.attr} retirée")
        return EXIT_OK
    printer.fail(f"{args.attr} n'était pas épinglé")
    return EXIT_ERROR


def cmd_pins(args, engine: Engine, printer: Printer) -> int:
    pins = engine.resolver.pins()
    if getattr(args, "json", False):
        printer.data(pins)
        return EXIT_OK
    if not pins:
        printer.warn(f"aucune épingle (fichier {engine.resolver.pins_file})")
        return EXIT_OK
    for attr, path in sorted(pins.items()):
        printer.out(f"  {attr:<30} {path}")
    return EXIT_OK


def cmd_init(args, engine: Engine, printer: Printer) -> int:
    target = Path(getattr(args, "store", None) or "/nix/store")
    printer.title("npkg init")
    printer.kv("store visé", str(target))
    printer.kv("store actuel", str(engine.store.root))
    if target.exists():
        writable = os.access(target, os.W_OK)
        printer.out(f"  {'✓' if writable else '✗'} {target} existe (écrivable : {writable})")
        return EXIT_OK if writable else EXIT_ERROR
    command = f"sudo sh -c 'mkdir -p {target} && chown {os.getuid()}:{os.getgid()} {target}'"
    printer.out("  nécessaire pour que les binaires nixpkgs trouvent leur interpréteur :")
    printer.out(f"  {printer.bold}{command}{printer.reset}")
    if not args.yes:
        printer.warn("relancez avec --yes pour exécuter")
        return EXIT_OK
    if not shutil.which("sudo"):
        printer.fail("sudo introuvable : créez le répertoire vous-même")
        return EXIT_ERROR
    subprocess.run(["sudo", "sh", "-c", f"mkdir -p {target} && chown {os.getuid()}:{os.getgid()} {target}"], check=True)
    printer.ok(f"{target} prêt et à vous")
    return EXIT_OK


def cmd_nar(args, engine: Engine, printer: Printer) -> int:
    path = Path(args.path).expanduser()
    if args.action in ("dump", "hash"):
        if not path.is_dir():
            printer.fail(f"{path} doit être un répertoire")
            return EXIT_ERROR
        data = nar_mod.dump_dir(path)
        if args.action == "dump":
            sys.stdout.buffer.write(data)
            return EXIT_OK
        printer.out("sha256:" + sha256_bytes(data).split(":", 1)[1])
        return EXIT_OK
    if not path.is_file():
        printer.fail(f"{path} doit être un fichier NAR")
        return EXIT_ERROR
    with path.open("rb") as fh:
        tree = nar_mod.read_tree(fh)
        for rel, node in nar_mod.walk_tree(tree):
            kind = node["type"]
            size = len(node.get("contents", b"")) if kind == "regular" else 0
            extra = f" -> {node['target']}" if kind == "symlink" else ""
            printer.out(f"  {kind:<8} {size:>9} {rel}{extra}")
    return EXIT_OK


def cmd_completion(args, engine: Engine, printer: Printer) -> int:
    if args.shell == "zsh":
        print(
            "#compdef npkg\n_npkg() {\n"
            f"  local -a cmds; cmds=({COMMANDS})\n"
            "  _describe 'commande npkg' cmds\n}\ncompdef _npkg npkg"
        )
    else:
        print(
            "_npkg() {\n"
            '  local cur="${COMP_WORDS[COMP_CWORD]}"\n'
            f'  local cmds="{COMMANDS}"\n'
            '  COMPREPLY=( $(compgen -W "$cmds" -- "$cur") )\n}\ncomplete -F _npkg npkg'
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# entrée
# ---------------------------------------------------------------------------

GLOBAL_DEFAULTS: dict[str, object] = {
    "home": None, "store_dir": None, "channel": None, "system": None, "jobset": None,
    "cache": None, "jobs": None, "no_verify": False, "dry_run": False, "refresh": False,
    "json": False, "quiet": False, "no_nix": False,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # les options globales sont en default=SUPPRESS (pour que `npkg --json list` et
    # `npkg list --json` se valent) : on remet ici celles qui n'ont pas été données.
    for key, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    printer = Printer(json_mode=getattr(args, "json", False), quiet=getattr(args, "quiet", False))
    handler: Callable | None = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        engine = make_engine(args, printer)
    except OSError as exc:
        printer.fail(f"état npkg inaccessible : {exc}")
        return EXIT_ERROR
    try:
        return handler(args, engine, printer) or EXIT_OK
    except LockBusy as exc:
        printer.fail(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        printer.fail("interrompu (le store n'a pas été modifié en cours de téléchargement)")
        return 130
    except (FetchError, NotFound, LookupError, ValueError, OSError) as exc:
        if os.environ.get("NPKG_DEBUG"):
            raise
        printer.fail(str(exc))
        return EXIT_ERROR


__all__ = ["main", "build_parser", "Printer"]
