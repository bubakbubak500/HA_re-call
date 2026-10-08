import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type ImportResult,
  importSummary,
  importTree,
  isZip,
  itemsFromPicked,
  MAX_FILE_BYTES,
  MAX_UPLOAD_BYTES,
  planDrop,
  walkDirectory,
  zipBaseName,
} from "@/lib/import-tree";

// ── Minimal FileSystem API fakes (node has no DataTransfer / entries) ────────

type FakeNode = { [name: string]: string | FakeNode };

function fileEntry(name: string, content: string): FileSystemFileEntry {
  return {
    name,
    isFile: true,
    isDirectory: false,
    file: (ok: (f: File) => void) => ok(new File([content], name)),
  } as unknown as FileSystemFileEntry;
}

function dirEntry(name: string, children: FakeNode): FileSystemDirectoryEntry {
  const entries = Object.entries(children).map(([n, v]) =>
    typeof v === "string" ? fileEntry(n, v) : dirEntry(n, v),
  );
  return {
    name,
    isFile: false,
    isDirectory: true,
    createReader: () => {
      // Hand entries out in pages of 2, like the browser's ~100-per-call limit.
      let i = 0;
      return {
        readEntries: (ok: (batch: FileSystemEntry[]) => void) => {
          const batch = entries.slice(i, i + 2);
          i += 2; // advance first: the callback re-enters synchronously
          ok(batch);
        },
      };
    },
  } as unknown as FileSystemDirectoryEntry;
}

function dataTransfer(
  items: ({ dir: FileSystemDirectoryEntry } | { file: File })[],
): DataTransfer {
  return {
    files: items.flatMap((i) => ("file" in i ? [i.file] : [])),
    items: items.map((i) => ({
      kind: "file",
      webkitGetAsEntry: () => ("dir" in i ? i.dir : { isDirectory: false }),
      getAsFile: () => ("file" in i ? i.file : null),
    })),
  } as unknown as DataTransfer;
}

const result = (r: Partial<ImportResult>): ImportResult => ({
  folder_id: null,
  folders: 0,
  notes: 0,
  skipped: 0,
  first_note_id: null,
  ...r,
});

describe("import-tree", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("recognizes zips and names imports after them", () => {
    expect(isZip(new File(["x"], "Notes.ZIP"))).toBe(true);
    expect(isZip(new File(["x"], "blob", { type: "application/x-zip-compressed" }))).toBe(true);
    expect(isZip(new File(["x"], "a.md"))).toBe(false);
    expect(zipBaseName("Personal.zip")).toBe("Personal");
    expect(zipBaseName(".zip")).toBe("Imported");
  });

  it("splits a drop into folders, zips and loose files", () => {
    const dir = dirEntry("Vault", {});
    const zip = new File(["x"], "Export.zip");
    const md = new File(["x"], "a.md");
    const plan = planDrop(dataTransfer([{ dir }, { file: zip }, { file: md }]));
    expect(plan.dirs).toEqual([dir]);
    expect(plan.zips).toEqual([zip]);
    expect(plan.files).toEqual([md]);
  });

  it("walks a folder: relative paths, empty dirs, skips non-text and tool state", async () => {
    const items = await walkDirectory(
      dirEntry("Vault", {
        "Inbox.md": "hello",
        Projects: { "Plan.md": "plan", "spec.pdf": "%PDF", Archive: {} },
        ".obsidian": { "workspace.json": "{}" },
        "photo.png": "png",
      }),
    );
    expect(items).toEqual(
      expect.arrayContaining([
        { path: "Inbox.md", body: "hello" },
        { path: "Projects/Plan.md", body: "plan" },
        { path: "Projects/spec.pdf", skip: true },
        { path: "Projects/Archive", body: null },
        { path: "photo.png", skip: true },
      ]),
    );
    expect(items.some((i) => i.path.startsWith(".obsidian"))).toBe(false);
    expect(items).toHaveLength(5);
  });

  it("skips (doesn't read) notes over the per-file cap", async () => {
    const items = await walkDirectory(
      dirEntry("V", { "big.md": "x".repeat(MAX_FILE_BYTES + 1), "ok.md": "ok" }),
    );
    expect(items).toEqual(
      expect.arrayContaining([{ path: "big.md", skip: true }, { path: "ok.md", body: "ok" }]),
    );
  });

  it("builds items from a folder picker's relative paths", async () => {
    const pick = (path: string, content: string) => {
      const f = new File([content], path.split("/").pop()!);
      Object.defineProperty(f, "webkitRelativePath", { value: path });
      return f;
    };
    const { root, items } = await itemsFromPicked([
      pick("Vault/a.md", "a"),
      pick("Vault/Sub/b.md", "b"),
      pick("Vault/.git/config", "x"),
    ]);
    expect(root).toBe("Vault");
    expect(items).toEqual([
      { path: "a.md", body: "a" },
      { path: "Sub/b.md", body: "b" },
    ]);
  });

  it("uploads a zip as-is and items as JSON, and surfaces server errors", async () => {
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) =>
      new Response(JSON.stringify(result({ notes: 2 })), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const zip = new File(["PK"], "Export.zip");
    await importTree("p1", { folderId: "f1", name: "Export" }, zip);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/projects/p1/import?folder_id=f1&name=Export");
    expect(init.body).toBe(zip);
    expect((init.headers as Record<string, string>)["content-type"]).toBe("application/zip");

    await importTree("p1", { folderId: null, name: null }, [{ path: "a.md", body: "a" }]);
    const [url2, init2] = fetchMock.mock.calls[1];
    expect(url2).toBe("/api/projects/p1/import?");
    expect(JSON.parse(init2.body as string)).toEqual({ items: [{ path: "a.md", body: "a" }] });

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: "That file isn't a valid zip." }), { status: 400 }),
    );
    await expect(importTree("p1", { folderId: null, name: null }, zip)).rejects.toThrow(
      "That file isn't a valid zip.",
    );
  });

  it("refuses an over-limit upload before sending it", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const big = new File([new Uint8Array(MAX_UPLOAD_BYTES + 1)], "big.zip");
    await expect(importTree("p1", { folderId: null, name: null }, big)).rejects.toThrow(
      /too large/,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("summarizes one or more imports", () => {
    expect(importSummary([result({ notes: 1 })])).toBe("Imported 1 note.");
    expect(
      importSummary([result({ notes: 3, folders: 2, skipped: 1 }), result({ notes: 2, folders: 1 })]),
    ).toBe("Imported 5 notes in 3 folders · skipped 1 file (only .md and .txt are imported).");
  });
});
