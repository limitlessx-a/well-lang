/**
 * 工作区符号索引：扫描工作区内所有 `.we` 文件，收集顶层声明，
 * 为“自动导入 / 补全 / 整理导入”提供查询。
 *
 * 设计要点：
 *  - `refresh()` 异步并发读取（`vscode.workspace.fs.readFile` + `TextDecoder`），
 *    不阻塞 activate；未完成时 `findDeclarations` 返回当前已有结果。
 *  - `declsOfFile()` 同步缓存查询；未索引到的文件（例如 import 指向工作区之外）
 *    按需同步读取一次并缓存，读不到返回 null。
 *  - `FileSystemWatcher` 监听工作区内所有 `.we`，处理 create/change/delete 增量更新；
 *    change 做 300ms 防抖合并。
 *  - 同一文件只解析一次并缓存。
 */

import * as fs from "fs";
import * as path from "path";
import { TextDecoder } from "util";
import * as vscode from "vscode";
import { DeclInfo, parseDeclarations } from "./wellParse";

export interface IndexedDecl {
  name: string;
  kind: DeclInfo["kind"];
  /** 文件绝对路径。 */
  file: string;
  line: number;
}

/** 无论用户如何配置，都不索引这些目录（相对工作区）。 */
const ALWAYS_EXCLUDED_SEGMENTS = new Set(["node_modules", "build", "out", ".git", ".svn"]);

/** 用户没配置 `well.index.exclude` 时的默认排除（与 package.json 默认值一致）。 */
const DEFAULT_EXCLUDE = [
  "**/node_modules/**",
  "**/build/**",
  "**/out/**",
  "**/.git/**",
  "**/.svn/**",
];

const CHANGE_DEBOUNCE_MS = 300;
const INDEX_CONFIG_SECTION = "well";
const INDEX_CONFIG_KEY = "index.exclude";

/** 路径索引键：Windows 大小写不敏感（import 解析出的路径与 findFiles 的大小写可能不同）。 */
function pathKey(file: string): string {
  const resolved = path.resolve(file);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function isExcludedPath(file: string): boolean {
  const resolved = path.resolve(file);
  const folders = vscode.workspace.workspaceFolders ?? [];
  for (const folder of folders) {
    const rel = path.relative(folder.uri.fsPath, resolved);
    if (rel.length === 0 || rel.startsWith("..") || path.isAbsolute(rel)) {
      continue;
    }
    for (const segment of rel.split(/[\\/]+/)) {
      if (ALWAYS_EXCLUDED_SEGMENTS.has(segment.toLowerCase())) {
        return true;
      }
    }
    return false;
  }
  // 不在任何工作区目录下（例如按需读取的工作区外文件）不做目录段排除
  return false;
}

export class WorkspaceIndex implements vscode.Disposable {
  /** 精确名 → 声明列表（同名可出现在多个文件）。 */
  private byName = new Map<string, IndexedDecl[]>();
  /** 路径键 → 该文件的顶层声明（已解析但无声明 = 空数组，与“未索引”区分）。 */
  private byFile = new Map<string, IndexedDecl[]>();
  private readonly disposables: vscode.Disposable[] = [];
  private readonly watcher: vscode.FileSystemWatcher;
  private readonly decoder = new TextDecoder("utf-8");
  /** refresh 竞态令牌：只有最后一次 refresh 的结果会写入索引。 */
  private refreshToken = 0;
  /** 待增量重索引的文件（防抖合并 change/create 事件）。 */
  private pending = new Map<string, vscode.Uri>();
  private pendingTimer: NodeJS.Timeout | undefined;
  private disposed = false;

  constructor() {
    this.watcher = vscode.workspace.createFileSystemWatcher("**/*.we");
    this.disposables.push(
      this.watcher,
      this.watcher.onDidCreate((uri) => this.scheduleReindex(uri)),
      this.watcher.onDidChange((uri) => this.scheduleReindex(uri)),
      this.watcher.onDidDelete((uri) => this.removeFile(uri.fsPath)),
      vscode.workspace.onDidChangeWorkspaceFolders(() => {
        void this.refresh();
      }),
      vscode.workspace.onDidChangeConfiguration((event) => {
        if (event.affectsConfiguration(`${INDEX_CONFIG_SECTION}.${INDEX_CONFIG_KEY}`)) {
          void this.refresh();
        }
      })
    );
  }

  /**
   * 全量重建索引。调用方无需 await（activate 中可后台执行）；
   * 并发调用时只有最后一次生效。
   */
  async refresh(): Promise<void> {
    if (this.disposed) {
      return;
    }
    const token = (this.refreshToken += 1);
    const exclude = this.excludeGlob();

    let uris: vscode.Uri[];
    try {
      uris = await vscode.workspace.findFiles("**/*.we", exclude);
    } catch {
      return;
    }
    if (this.disposed || token !== this.refreshToken) {
      return;
    }

    // 去重 + 排除总是要排除的目录
    const files: string[] = [];
    const seenFiles = new Set<string>();
    for (const uri of uris) {
      const file = uri.fsPath;
      if (!file.toLowerCase().endsWith(".we") || isExcludedPath(file)) {
        continue;
      }
      const key = pathKey(file);
      if (seenFiles.has(key)) {
        continue;
      }
      seenFiles.add(key);
      files.push(file);
    }

    // 并发读取；单个文件失败跳过（保留旧数据）
    const results = await Promise.all(
      files.map(async (file) => {
        const decls = await this.readDecls(file);
        return { file, decls };
      })
    );
    if (this.disposed || token !== this.refreshToken) {
      return;
    }

    const nextByName = new Map<string, IndexedDecl[]>();
    const nextByFile = new Map<string, IndexedDecl[]>();
    for (const { file, decls } of results) {
      if (decls === null) {
        // 临时 IO 失败：保留上一次索引到的数据
        const old = this.byFile.get(pathKey(file));
        if (old !== undefined) {
          this.addToIndex(nextByName, nextByFile, file, old);
        }
        continue;
      }
      this.addToIndex(nextByName, nextByFile, file, decls);
    }

    this.byName = nextByName;
    this.byFile = nextByFile;
  }

  /** 精确名匹配（大小写敏感），未索引到时返回空数组。 */
  findDeclarations(name: string): IndexedDecl[] {
    const decls = this.byName.get(name);
    return decls === undefined ? [] : decls.slice();
  }

  /** 当前索引到的全部声明（用于补全列表遍历）。 */
  get declarations(): IndexedDecl[] {
    const all: IndexedDecl[] = [];
    for (const decls of this.byFile.values()) {
      for (const decl of decls) {
        all.push(decl);
      }
    }
    return all;
  }

  /**
   * 返回文件的顶层声明名；未索引到的文件同步读取一次并缓存。
   * 文件不存在/读不到时返回 null（用于 imports.ImportContext.declsOf）。
   */
  declsOfFile(absPath: string): string[] | null {
    if (!absPath) {
      return null;
    }
    const resolved = path.resolve(absPath);
    const key = pathKey(resolved);
    const cached = this.byFile.get(key);
    if (cached !== undefined) {
      return cached.map((decl) => decl.name);
    }
    let source: string;
    try {
      source = fs.readFileSync(resolved, "utf8");
    } catch {
      return null;
    }
    const decls = this.toIndexedDecls(source, resolved);
    // 按需读取的文件也进入缓存（既供 declsOfFile，也供补全）
    this.byFile.set(key, decls);
    for (const decl of decls) {
      const list = this.byName.get(decl.name);
      if (list === undefined) {
        this.byName.set(decl.name, [decl]);
      } else {
        list.push(decl);
      }
    }
    return decls.map((decl) => decl.name);
  }

  /** 已索引的文件数。 */
  get size(): number {
    return this.byFile.size;
  }

  dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.refreshToken += 1;
    if (this.pendingTimer !== undefined) {
      clearTimeout(this.pendingTimer);
      this.pendingTimer = undefined;
    }
    this.pending.clear();
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.disposables.length = 0;
    this.byName.clear();
    this.byFile.clear();
  }

  // -------------------------------------------------------------------------
  // 内部实现
  // -------------------------------------------------------------------------

  private async readDecls(file: string): Promise<IndexedDecl[] | null> {
    try {
      const bytes = await vscode.workspace.fs.readFile(vscode.Uri.file(file));
      // TextDecoder 默认忽略并去除 UTF-8 BOM
      return this.toIndexedDecls(this.decoder.decode(bytes), file);
    } catch {
      return null;
    }
  }

  private toIndexedDecls(source: string, file: string): IndexedDecl[] {
    const absolute = path.resolve(file);
    const parsed = parseDeclarations(source);
    const decls: IndexedDecl[] = [];
    for (const decl of parsed) {
      decls.push({ name: decl.name, kind: decl.kind, file: absolute, line: decl.line });
    }
    return decls;
  }

  private addToIndex(
    nameIndex: Map<string, IndexedDecl[]>,
    fileIndex: Map<string, IndexedDecl[]>,
    file: string,
    decls: IndexedDecl[]
  ): void {
    fileIndex.set(pathKey(file), decls);
    for (const decl of decls) {
      const list = nameIndex.get(decl.name);
      if (list === undefined) {
        nameIndex.set(decl.name, [decl]);
      } else {
        list.push(decl);
      }
    }
  }

  /** 由配置数组拼出 findFiles 的 exclude glob（`{a,b,c}`）。 */
  private excludeGlob(): string | undefined {
    let configured: unknown;
    try {
      configured = vscode.workspace
        .getConfiguration(INDEX_CONFIG_SECTION)
        .get<string[]>(INDEX_CONFIG_KEY);
    } catch {
      configured = undefined;
    }
    let patterns: string[];
    if (Array.isArray(configured)) {
      patterns = [];
      for (const pattern of configured) {
        if (typeof pattern === "string" && pattern.trim().length > 0) {
          patterns.push(withForwardSlashes(pattern.trim()));
        }
      }
    } else {
      patterns = DEFAULT_EXCLUDE.slice();
    }
    if (patterns.length === 0) {
      // 用户显式配置了空数组时也保留默认排除（避免把 node_modules 全索引进来）
      patterns = DEFAULT_EXCLUDE.slice();
    }
    if (patterns.length === 1) {
      return patterns[0];
    }
    return `{${patterns.join(",")}}`;
  }

  private scheduleReindex(uri: vscode.Uri): void {
    if (this.disposed || isExcludedPath(uri.fsPath)) {
      return;
    }
    this.pending.set(pathKey(uri.fsPath), uri);
    if (this.pendingTimer !== undefined) {
      return;
    }
    this.pendingTimer = setTimeout(() => {
      this.pendingTimer = undefined;
      void this.flushPending();
    }, CHANGE_DEBOUNCE_MS);
  }

  private async flushPending(): Promise<void> {
    if (this.disposed) {
      return;
    }
    const entries = Array.from(this.pending.values());
    this.pending.clear();
    for (const uri of entries) {
      if (this.disposed) {
        return;
      }
      const file = uri.fsPath;
      if (isExcludedPath(file)) {
        this.removeFile(file);
        continue;
      }
      const decls = await this.readDecls(file);
      if (decls === null) {
        this.removeFile(file);
        continue;
      }
      this.replaceFile(file, decls);
    }
  }

  private replaceFile(file: string, decls: IndexedDecl[]): void {
    this.removeFile(file);
    this.byFile.set(pathKey(file), decls);
    for (const decl of decls) {
      const list = this.byName.get(decl.name);
      if (list === undefined) {
        this.byName.set(decl.name, [decl]);
      } else {
        list.push(decl);
      }
    }
  }

  private removeFile(file: string): void {
    const key = pathKey(file);
    const old = this.byFile.get(key);
    if (old === undefined) {
      return;
    }
    this.byFile.delete(key);
    for (const decl of old) {
      const list = this.byName.get(decl.name);
      if (list === undefined) {
        continue;
      }
      const filtered = list.filter((candidate) => candidate !== decl);
      if (filtered.length === 0) {
        this.byName.delete(decl.name);
      } else {
        this.byName.set(decl.name, filtered);
      }
    }
  }
}

function withForwardSlashes(value: string): string {
  return value.split("\\").join("/");
}
