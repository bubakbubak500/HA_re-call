"""Parse an uploaded folder tree (a .zip, or files walked in the browser) into
the folders + notes an import creates.

Mirrors the export (`data.get_export_tree`): directories become folders and each
markdown/text file becomes a note, so exporting a workspace and importing the
zip elsewhere rebuilds the same structure. Pure functions — the database side
is `data.import_tree`.

Everything here treats the upload as untrusted: paths are normalized and any
`..` / absolute / drive path is dropped (no escaping the target), sizes are
capped before anything is decompressed (zip bombs), and only text files are
read.
"""
from __future__ import annotations

import io
import posixpath
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field

from .markdown import parse_frontmatter

# Request body cap. Also the practical ceiling of the web proxy, which buffers
# request bodies for its middleware at about this size.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024  # all notes, uncompressed
MAX_FILE_BYTES = 2 * 1024 * 1024  # one note
MAX_FILES = 5000

NOTE_EXTENSIONS = (".md", ".markdown", ".txt")
# Tool metadata that rides along in zips and vaults; never user content.
_IGNORED_NAMES = {"__MACOSX", "Thumbs.db", "desktop.ini"}


class ImportRejected(ValueError):
    """The upload can't be imported at all (not a zip, over a limit). The
    message is safe to show the user."""


@dataclass
class ImportNote:
    folder: tuple[str, ...]  # directory path below the import root
    title: str
    body: str


@dataclass
class ImportTree:
    folders: set[tuple[str, ...]] = field(default_factory=set)
    notes: list[ImportNote] = field(default_factory=list)
    # Files the user might expect but that weren't imported: not markdown/text,
    # over the per-file cap, or an unsafe path. Tool clutter isn't counted.
    skipped: int = 0


_UNSAFE = "unsafe"
_IGNORED = "ignored"


def _clean_parts(path: str) -> tuple[str, ...] | str:
    """Split an upload path into safe components. Returns _UNSAFE for paths that
    try to leave the import root (absolute, drive-qualified, `..`) and _IGNORED
    for tool clutter (dotfiles/dot-dirs like .obsidian or .git, __MACOSX)."""
    p = path.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return _UNSAFE
    parts: list[str] = []
    for raw in p.split("/"):
        part = raw.strip()
        if part in ("", "."):
            continue
        if part == "..":
            return _UNSAFE
        if part in _IGNORED_NAMES or part.startswith("."):
            return _IGNORED
        parts.append(part)
    return tuple(parts)


def _is_note_file(name: str) -> bool:
    return name.lower().endswith(NOTE_EXTENSIONS)


def _decode(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text[1:] if text.startswith("﻿") else text


def note_title(body: str, filename: str) -> str:
    """Frontmatter `title:` if present, else the filename without extension.

    Unlike the single-file import (which prefers the first `# H1`), a tree
    import trusts the filename: recall exports and Obsidian-style vaults both
    name a note's file after its title, and `[[wikilinks]]` resolve by title —
    so keeping it keeps the links working."""
    fm, _ = parse_frontmatter(body)
    title = fm.get("title") if isinstance(fm, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    stem, _ = posixpath.splitext(filename)
    return stem.strip() or "Untitled"


def build_tree(
    entries: list[tuple[str, bytes | None]],
    root_name: str | None = None,
    unread: Sequence[str] = (),
) -> ImportTree:
    """Turn (path, content) pairs into an ImportTree. `content=None` marks a
    directory entry (kept, so empty folders survive). `unread` lists files
    skipped without reading them (a zip's non-text or oversized members).
    `root_name` is the name the import root will get (e.g. the zip's name);
    see _unwrap_single_root."""
    tree = ImportTree()
    total = 0
    # Directories that held files we skipped (attachments, PDFs…). A folder
    # with only those would arrive empty, so it's dropped unless it also has
    # notes; a directory that was empty in the upload is still kept.
    had_skipped: set[tuple[str, ...]] = set()
    for path in unread:
        parts = _clean_parts(path)
        if parts == _IGNORED:
            continue
        tree.skipped += 1
        if isinstance(parts, tuple):
            had_skipped.update(parts[:i] for i in range(1, len(parts)))
    for path, content in entries:
        parts = _clean_parts(path)
        if parts == _IGNORED or parts == ():
            continue
        if parts == _UNSAFE:
            tree.skipped += content is not None
            continue
        assert isinstance(parts, tuple)
        if content is None:
            tree.folders.add(parts)
            continue
        name = parts[-1]
        if not _is_note_file(name) or len(content) > MAX_FILE_BYTES:
            tree.skipped += 1
            had_skipped.update(parts[:i] for i in range(1, len(parts)))
            continue
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ImportRejected("The import is too large (over 50 MB of notes).")
        body = _decode(content)
        tree.notes.append(ImportNote(parts[:-1], note_title(body, name), body))
        tree.folders.add(parts[:-1])
    with_notes = {n.folder[:i] for n in tree.notes for i in range(1, len(n.folder) + 1)}
    tree.folders = {f for f in tree.folders if f in with_notes or f not in had_skipped}
    # Every ancestor of a kept folder is a folder too.
    for f in list(tree.folders):
        for i in range(1, len(f)):
            tree.folders.add(f[:i])
    tree.folders.discard(())
    if root_name:
        _unwrap_single_root(tree, root_name)
    return tree


def _unwrap_single_root(tree: ImportTree, root_name: str) -> None:
    """If everything lives under one top-level directory named like the import
    root itself (and nothing sits beside it), drop that level: "Notes.zip"
    made by compressing a "Notes" folder holds "Notes/…", and must not become
    Notes/Notes/…. A differently named single folder is real structure (a
    recall export of a workspace whose only content is one folder) and stays."""
    if not tree.folders or any(not n.folder for n in tree.notes):
        return
    tops = {f[0] for f in tree.folders}
    if len(tops) != 1:
        return
    (top,) = tops
    if top.casefold() != root_name.strip().casefold():
        return
    tree.folders = {f[1:] for f in tree.folders if len(f) > 1}
    for n in tree.notes:
        n.folder = n.folder[1:]


def parse_zip(data: bytes, root_name: str | None = None) -> ImportTree:
    """Read a zip upload. Checks declared sizes before decompressing anything,
    and reads only the members that will become notes."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportRejected("That file isn't a valid zip.") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise ImportRejected(f"The zip has too many files (over {MAX_FILES}).")
        wanted = [i for i in infos if not i.is_dir() and _is_note_file(i.filename)
                  and i.file_size <= MAX_FILE_BYTES]
        if sum(i.file_size for i in wanted) > MAX_TOTAL_BYTES:
            raise ImportRejected("The import is too large (over 50 MB of notes).")
        entries: list[tuple[str, bytes | None]] = []
        unread: list[str] = []  # non-text or oversized: never decompressed
        for info in infos:
            if info.is_dir():
                entries.append((info.filename, None))
            elif info in wanted:
                # Read at most one byte past the cap, so a member whose header
                # under-states its size can't inflate past it.
                with zf.open(info) as fh:
                    entries.append((info.filename, fh.read(MAX_FILE_BYTES + 1)))
            else:
                unread.append(info.filename)
    return build_tree(entries, root_name, unread)


def parse_items(items: object) -> ImportTree:
    """Read the JSON form (`[{"path": ..., "body": ...}]`) the web app sends
    for a dropped folder, with paths relative to that folder (so there is no
    wrapper level to unwrap). A null/absent body marks a directory;
    `"skip": true` marks a file the browser didn't read (not text, too big)."""
    if not isinstance(items, list):
        raise ImportRejected("Expected a list of files.")
    if len(items) > MAX_FILES:
        raise ImportRejected(f"Too many files (over {MAX_FILES}).")
    entries: list[tuple[str, bytes | None]] = []
    unread: list[str] = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("path"), str):
            continue
        if it.get("skip") is True:
            unread.append(it["path"])
            continue
        body = it.get("body")
        entries.append((it["path"], body.encode("utf-8") if isinstance(body, str) else None))
    return build_tree(entries, unread=unread)
