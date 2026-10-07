// Folder-tree import — the inverse of the zip export. A dropped (or picked)
// folder, or a .zip, becomes folders + notes via one backend call
// (POST /api/projects/{id}/import), which does the parsing, safety checks and
// the single transaction. This module only gathers what was dropped.

// Mirrors the backend caps (src/importer.py): an over-limit upload is refused
// here, before it is sent, with the same message the server would give.
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_FILE_BYTES = 2 * 1024 * 1024;

const NOTE_RE = /\.(md|markdown|txt)$/i;
const ZIP_RE = /\.zip$/i;
// Tool state that rides along in folders and vaults — never walked or read.
const IGNORED = new Set(["__MACOSX", "Thumbs.db", "desktop.ini"]);
const isIgnored = (name: string) => name.startsWith(".") || IGNORED.has(name);

// One file or directory of a folder import, relative to the dropped folder.
// `body: null` is a directory; `skip` a file that exists but wasn't read
// (not text, or too big), so the import can report it.
export type TreeItem = { path: string; body?: string | null; skip?: true };

export type ImportResult = {
  folder_id: string | null;
  folders: number;
  notes: number;
  skipped: number;
  first_note_id: string | null;
};

export class ImportFailed extends Error {}

export const isZip = (f: File) =>
  ZIP_RE.test(f.name) ||
  f.type === "application/zip" ||
  f.type === "application/x-zip-compressed";

// The folder/workspace name an import gets: the zip's name without ".zip".
export const zipBaseName = (name: string) => name.replace(ZIP_RE, "").trim() || "Imported";

// What one OS drop carries, split by how each part imports.
export type DropPlan = {
  dirs: FileSystemDirectoryEntry[]; // dropped folders → folder import
  zips: File[]; // .zip files → zip import
  files: File[]; // anything else → the flat markdown import
};

// Must run synchronously inside the drop handler: the browser empties
// DataTransfer once the event handler returns (i.e. after the first await).
export function planDrop(dt: DataTransfer): DropPlan {
  const plan: DropPlan = { dirs: [], zips: [], files: [] };
  const items = Array.from(dt.items ?? []).filter((i) => i.kind === "file");
  if (items.length === 0) {
    for (const f of Array.from(dt.files)) (isZip(f) ? plan.zips : plan.files).push(f);
    return plan;
  }
  for (const item of items) {
    const entry = item.webkitGetAsEntry?.();
    if (entry?.isDirectory) {
      plan.dirs.push(entry as FileSystemDirectoryEntry);
      continue;
    }
    const f = item.getAsFile();
    if (f) (isZip(f) ? plan.zips : plan.files).push(f);
  }
  return plan;
}

export const isEmptyPlan = (p: DropPlan) =>
  p.dirs.length + p.zips.length + p.files.length === 0;

async function toItem(file: File, path: string): Promise<TreeItem> {
  if (!NOTE_RE.test(file.name) || file.size > MAX_FILE_BYTES) return { path, skip: true };
  return { path, body: await file.text() };
}

// readEntries returns at most ~100 entries per call; read until it's empty.
function readAll(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => {
    const out: FileSystemEntry[] = [];
    const next = () =>
      reader.readEntries((batch) => {
        if (batch.length === 0) return resolve(out);
        out.push(...batch);
        next();
      }, reject);
    next();
  });
}

const fileOf = (entry: FileSystemFileEntry) =>
  new Promise<File>((resolve, reject) => entry.file(resolve, reject));

// Walk a dropped folder into import items, with paths relative to it (the
// folder itself becomes the import's root folder, named by the caller).
export async function walkDirectory(dir: FileSystemDirectoryEntry): Promise<TreeItem[]> {
  const items: TreeItem[] = [];
  async function visit(d: FileSystemDirectoryEntry, prefix: string) {
    const children = (await readAll(d.createReader())).filter((c) => !isIgnored(c.name));
    if (children.length === 0 && prefix) items.push({ path: prefix, body: null });
    for (const c of children) {
      const path = prefix ? `${prefix}/${c.name}` : c.name;
      if (c.isDirectory) await visit(c as FileSystemDirectoryEntry, path);
      else items.push(await toItem(await fileOf(c as FileSystemFileEntry), path));
    }
  }
  await visit(dir, "");
  return items;
}

// The same, for files from an <input webkitdirectory> picker: each carries
// "Root/sub/a.md" in webkitRelativePath. Returns the root folder's name and
// the items relative to it. (Empty directories aren't reported by pickers.)
export async function itemsFromPicked(
  files: File[],
): Promise<{ root: string; items: TreeItem[] }> {
  let root = "";
  const items: TreeItem[] = [];
  for (const f of files) {
    const parts = (f.webkitRelativePath || f.name).split("/");
    root ||= parts.length > 1 ? parts[0] : "";
    const rel = parts.length > 1 ? parts.slice(1) : parts;
    if (rel.some(isIgnored)) continue;
    items.push(await toItem(f, rel.join("/")));
  }
  return { root: root || "Imported", items };
}

// Upload one tree: a zip as-is, or folder items as JSON. `name` puts the
// import in a new folder of that name under `folderId` (null = workspace
// root); without it the contents land directly in the target.
export async function importTree(
  projectId: string,
  target: { folderId: string | null; name: string | null },
  payload: File | TreeItem[],
): Promise<ImportResult> {
  const isFile = payload instanceof File;
  const body = isFile ? payload : JSON.stringify({ items: payload });
  const size = isFile ? payload.size : new Blob([body as string]).size;
  if (size > MAX_UPLOAD_BYTES) {
    throw new ImportFailed("The import is too large (over 10 MB). Split it into smaller parts.");
  }
  const qs = new URLSearchParams();
  if (target.folderId) qs.set("folder_id", target.folderId);
  if (target.name) qs.set("name", target.name);
  const r = await fetch(`/api/projects/${projectId}/import?${qs}`, {
    method: "POST",
    headers: { "content-type": isFile ? "application/zip" : "application/json" },
    body,
  });
  if (!r.ok) {
    const msg = ((await r.json().catch(() => null)) as { error?: string } | null)?.error;
    throw new ImportFailed(msg ?? `Import failed (HTTP ${r.status}).`);
  }
  return (await r.json()) as ImportResult;
}

// One toast line for a finished import (possibly several trees at once).
export function importSummary(results: ImportResult[]): string {
  const notes = results.reduce((n, r) => n + r.notes, 0);
  const folders = results.reduce((n, r) => n + r.folders, 0);
  const skipped = results.reduce((n, r) => n + r.skipped, 0);
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  let msg = `Imported ${plural(notes, "note")}`;
  if (folders) msg += ` in ${plural(folders, "folder")}`;
  if (skipped) msg += ` · skipped ${plural(skipped, "file")} (only .md and .txt are imported)`;
  return `${msg}.`;
}
