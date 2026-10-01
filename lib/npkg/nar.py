"""Lecteur / écrivain du format NAR (Nix Archive).

Le NAR est un format volontairement austère : chaque chaîne (et le contenu des
fichiers) est précédée de sa longueur en ``uint64`` little-endian, puis
complétée par des NUL jusqu'à un multiple de 8 octets.

    flux        : "nix-archive-1" <noeud>      (le noeud racine est un noeud comme les autres)
    répertoire  : "(" "type" "directory" ( "entry" "(" "name" <nom> "node" <noeud> ")" )* ")"
    fichier     : "(" "type" "regular" [ "executable" "" ] "contents" <octets> ")"
    lien        : "(" "type" "symlink" "target" <cible> ")"

Vérifié sur un NAR réel de ``cache.nixos.org`` : la racine porte bien
``"type" "directory"``, comme n'importe quel autre répertoire (le lecteur
tolère toutefois une racine sans ``type``, forme que quelques outils produisent).

Chaque paquet binaire d'un cache Nix est un NAR : npkg le décompresse, vérifie
son ``NarHash``, puis le déploie dans le store.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

MAGIC = "nix-archive-1"
MAX_STRING = 1 << 26  # 64 Mio : au-delà, un *nom* de champ est corrompu


class NarError(ValueError):
    pass


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

def _exact(fh: BinaryIO, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = fh.read(n - len(buf))
        if not chunk:
            raise NarError(f"NAR tronqué : encore {n - len(buf)} octets attendus")
        buf += chunk
    return bytes(buf)


class _Tok:
    """Tokenizer NAR avec un jeton de rappel (``peek``)."""

    def __init__(self, fh: BinaryIO):
        self.fh = fh
        self._pending: str | None = None

    def next(self) -> str:
        if self._pending is not None:
            token, self._pending = self._pending, None
            return token
        size = int.from_bytes(_exact(self.fh, 8), "little")
        if size > MAX_STRING:
            raise NarError(f"taille de champ aberrante : {size}")
        data = _exact(self.fh, size)
        _exact(self.fh, (-size) % 8)
        return data.decode("utf-8", "surrogateescape")

    def peek(self) -> str:
        token = self.next()
        self._pending = token
        return token

    def expect(self, expected: str) -> None:
        token = self.next()
        if token != expected:
            raise NarError(f"NAR invalide : attendu {expected!r}, lu {token!r}")


def _read_bytes(tok: _Tok) -> bytes:
    size = int.from_bytes(_exact(tok.fh, 8), "little")
    data = _exact(tok.fh, size)
    _exact(tok.fh, (-size) % 8)
    return data


def _write_str(out: bytearray, text: str | bytes) -> None:
    data = text.encode("utf-8") if isinstance(text, str) else text
    out += len(data).to_bytes(8, "little")
    out += data
    out += b"\x00" * ((-len(data)) % 8)


# ---------------------------------------------------------------------------
# lecture en mémoire (petits NAR, inspection, tests)
# ---------------------------------------------------------------------------

def read_tree(fh: BinaryIO) -> dict:
    tok = _Tok(fh)
    tok.expect(MAGIC)
    return _read_node(tok)


def _read_node(tok: _Tok) -> dict:
    tok.expect("(")
    head = tok.next()
    if head == ")":  # répertoire vide (racine sans type)
        return {"type": "directory", "entries": {}}
    if head == "entry":
        return _read_entries(tok, {"type": "directory", "entries": {}})
    if head != "type":
        raise NarError(f"jeton inattendu en début de noeud : {head!r}")
    kind = tok.next()
    if kind == "directory":
        return _read_entries(tok, {"type": "directory", "entries": {}})
    if kind == "regular":
        executable = False
        if tok.peek() in ("executable", "exec"):
            tok.next()
            tok.expect("")
            executable = True
        tok.expect("contents")
        contents = _read_bytes(tok)
        tok.expect(")")
        return {"type": "regular", "executable": executable, "contents": contents}
    if kind == "symlink":
        tok.expect("target")
        target = tok.next()
        tok.expect(")")
        return {"type": "symlink", "target": target}
    raise NarError(f"type de noeud inconnu : {kind!r}")


def _read_entries(tok: _Tok, node: dict) -> dict:
    while True:
        head = tok.next()
        if head == ")":
            return node
        if head != "entry":
            raise NarError(f"jeton inattendu dans un répertoire : {head!r}")
        tok.expect("(")
        tok.expect("name")
        name = tok.next()
        _safe_name(name)
        tok.expect("node")
        node["entries"][name] = _read_node(tok)
        tok.expect(")")


def _safe_name(name: str) -> str:
    if "/" in name or name in ("", ".", ".."):
        raise NarError(f"nom d'entrée dangereux refusé : {name!r}")
    return name


# ---------------------------------------------------------------------------
# extraction sur disque (utilisée par npkg install)
# ---------------------------------------------------------------------------

def extract(fh: BinaryIO, dest: Path | str) -> dict:
    """Déplie un NAR dans ``dest`` (créée) et renvoie des statistiques.

    Les bits d'exécution et les liens symboliques viennent du NAR lui-même ;
    c'est ce qui rend un binaire du store immédiatement exécutable.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    stats = {"files": 0, "dirs": 1, "symlinks": 0, "bytes": 0, "execs": 0}
    tok = _Tok(fh)
    tok.expect(MAGIC)
    _extract_node(tok, dest, stats, root=True)
    return stats


def _extract_entries(tok: _Tok, where: Path, stats: dict) -> None:
    while True:
        head = tok.next()
        if head == ")":
            return
        if head == "type":
            kind = tok.next()
            if kind != "directory":
                raise NarError(f"répertoire attendu, trouvé {kind!r}")
            continue
        if head != "entry":
            raise NarError(f"jeton inattendu dans un répertoire : {head!r}")
        tok.expect("(")
        tok.expect("name")
        name = _safe_name(tok.next())
        tok.expect("node")
        _extract_node(tok, where / name, stats)
        tok.expect(")")


def _extract_node(tok: _Tok, target: Path, stats: dict, *, root: bool = False) -> None:
    tok.expect("(")
    head = tok.peek()
    if head == "type":
        tok.next()
        kind = tok.next()
    elif head in ("entry", ")"):
        kind = "directory"
    else:
        raise NarError(f"jeton inattendu en début de noeud : {head!r}")
    if root and kind != "directory":
        raise NarError("la racine d'un NAR de paquet doit être un répertoire")

    if kind == "directory":
        target.mkdir(parents=True, exist_ok=True)
        stats["dirs"] += 1
        _extract_entries(tok, target, stats)  # consomme la parenthèse fermante
        return

    if kind == "regular":
        executable = False
        if tok.peek() in ("executable", "exec"):
            tok.next()
            tok.expect("")
            executable = True
        tok.expect("contents")
        size = int.from_bytes(_exact(tok.fh, 8), "little")
        _write_file(tok.fh, target, size, executable)
        _exact(tok.fh, (-size) % 8)
        stats["files"] += 1
        stats["bytes"] += size
        stats["execs"] += int(executable)
        tok.expect(")")
        return

    if kind == "symlink":
        tok.expect("target")
        link = tok.next()
        tok.expect(")")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        os.symlink(link, target)
        stats["symlinks"] += 1
        return

    raise NarError(f"type de noeud inconnu : {kind!r}")


def _write_file(fh: BinaryIO, target: Path, size: int, executable: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    remaining = size
    with target.open("wb") as out:
        while remaining:
            chunk = _exact(fh, min(remaining, 1 << 20))
            out.write(chunk)
            remaining -= len(chunk)
    mode = 0o755 if executable else 0o644
    os.chmod(target, mode)


# ---------------------------------------------------------------------------
# écriture (miroirs locaux, fixtures de test, `npkg repack`)
# ---------------------------------------------------------------------------

def dump_dir(path: Path | str) -> bytes:
    """Sérialise un répertoire au format NAR (comme ``nix-store --dump``)."""
    out = bytearray()
    _write_str(out, MAGIC)
    _write_str(out, "(")
    _write_str(out, "type")
    _write_str(out, "directory")
    _dump_entries(out, Path(path))
    _write_str(out, ")")
    return bytes(out)


def _dump_entries(out: bytearray, where: Path) -> None:
    for entry in sorted(Path(where).iterdir(), key=lambda p: p.name):
        _write_str(out, "entry")
        _write_str(out, "(")
        _write_str(out, "name")
        _write_str(out, entry.name)
        _write_str(out, "node")
        _dump_node(out, entry)
        _write_str(out, ")")


def _dump_node(out: bytearray, path: Path) -> None:
    _write_str(out, "(")
    if path.is_symlink():
        _write_str(out, "type")
        _write_str(out, "symlink")
        _write_str(out, "target")
        _write_str(out, os.readlink(path))
    elif path.is_dir():
        _write_str(out, "type")
        _write_str(out, "directory")
        _dump_entries(out, path)
        _write_str(out, ")")
        return
    else:
        _write_str(out, "type")
        _write_str(out, "regular")
        if path.stat().st_mode & stat.S_IXUSR:
            _write_str(out, "executable")
            _write_str(out, "")
        _write_str(out, "contents")
        data = path.read_bytes()
        out += len(data).to_bytes(8, "little")
        out += data
        out += b"\x00" * ((-len(data)) % 8)
    _write_str(out, ")")


def walk_tree(node: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """Aplatit un arbre NAR en ``[(chemin, noeud), ...]`` (trié)."""
    items: list[tuple[str, dict]] = []
    for name, child in sorted(node.get("entries", {}).items()):
        rel = f"{prefix}/{name}"
        items.append((rel, child))
        if child["type"] == "directory":
            items.extend(walk_tree(child, rel))
    return items
