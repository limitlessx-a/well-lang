/**
 * Go-to-definition (Ctrl+Click / F12):
 *  - top-level declarations (struct/event/controller/handler/func/namespace/static/enum/const)
 *  - type references, `new T(...)`, `extends Base`, `entry C`, `on(Handler)`
 *  - `import "..."` 路径 -> 打开被导入文件
 *  - `obj.field` / `Enum.MEMBER`（基于 var/参数/字段的简单类型推断）
 *  - 工作区其它文件中的声明（跨文件、未 import 也能跳）
 */
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import {
  isUserTypeName,
  parseFileInfo,
  ParsedFileInfo,
  simpleTypeName,
} from "./definitions";
import { isInsideStringOrComment, isWellDocument, wellEnabled } from "./util";
import { parseImports, resolveImportSpec } from "./wellParse";
import { WorkspaceIndex } from "./workspaceIndex";

interface Scope {
  /** 绝对文件路径。 */
  file: string;
  info: ParsedFileInfo;
}

interface StructDef {
  scope: Scope;
  name: string;
  kind: "struct" | "event";
}

const MAX_IMPORT_DEPTH = 4;
const MAX_BASE_DEPTH = 8;

function fileKey(file: string): string {
  const resolved = path.resolve(file);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function isSpaceChar(c: string): boolean {
  return c === " " || c === "\t" || c === "\n" || c === "\r";
}

function previousNonSpaceIndex(text: string, fromInclusive: number): number {
  let i = fromInclusive;
  while (i >= 0 && isSpaceChar(text[i])) {
    i -= 1;
  }
  return i;
}

function wordBefore(text: string, endExclusive: number): string | undefined {
  const match = /([A-Za-z_][A-Za-z0-9_]*)$/.exec(text.slice(0, endExclusive));
  return match ? match[1] : undefined;
}

function locationAt(file: string, line0: number): vscode.Location {
  const position = new vscode.Position(Math.max(0, line0), 0);
  return new vscode.Location(
    vscode.Uri.file(file),
    new vscode.Range(position, position)
  );
}

function dedupe(locations: vscode.Location[]): vscode.Location[] | undefined {
  const seen = new Set<string>();
  const result: vscode.Location[] = [];
  for (const location of locations) {
    const key = `${fileKey(location.uri.fsPath)}:${location.range.start.line}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    result.push(location);
  }
  return result.length > 0 ? result : undefined;
}

export class WellDefinitionProvider implements vscode.DefinitionProvider {
  private readonly fileCache = new Map<
    string,
    { mtimeMs: number; scope: Scope }
  >();
  private readonly docCache = new WeakMap<
    vscode.TextDocument,
    { version: number; scope: Scope }
  >();

  constructor(private readonly index: WorkspaceIndex) {}

  provideDefinition(
    document: vscode.TextDocument,
    position: vscode.Position
  ): vscode.Location[] | undefined {
    if (!wellEnabled() || !isWellDocument(document)) {
      return undefined;
    }
    const source = document.getText();
    const offset = document.offsetAt(position);

    const imported = this.importAt(source, document.uri.fsPath, offset);
    if (imported) {
      return [locationAt(imported, 0)];
    }
    if (isInsideStringOrComment(document, position)) {
      return undefined;
    }
    const range = document.getWordRangeAtPosition(
      position,
      /[A-Za-z_][A-Za-z0-9_]*/
    );
    if (!range) {
      return undefined;
    }
    const word = document.getText(range);
    if (!word) {
      return undefined;
    }
    const scope = this.documentScope(document);

    const wordStart = document.offsetAt(range.start);
    const beforeIndex = previousNonSpaceIndex(source, wordStart - 1);
    if (beforeIndex >= 0 && source[beforeIndex] === ".") {
      const base = wordBefore(source, previousNonSpaceIndex(source, beforeIndex - 1) + 1);
      if (base) {
        return this.resolveMember(scope, base, word);
      }
    }
    return this.resolveName(scope, word);
  }

  // -------------------------------------------------------------------------
  // 查询
  // -------------------------------------------------------------------------

  private resolveName(
    scope: Scope,
    word: string
  ): vscode.Location[] | undefined {
    const locations: vscode.Location[] = [];
    this.addLocalLocations(locations, scope, word);
    for (const imported of this.importClosure(scope, MAX_IMPORT_DEPTH)) {
      this.addDeclLocations(locations, imported, word);
    }
    for (const decl of this.index.findDeclarations(word)) {
      locations.push(locationAt(decl.file, decl.line - 1));
    }
    return dedupe(locations);
  }

  private resolveMember(
    scope: Scope,
    base: string,
    member: string
  ): vscode.Location[] | undefined {
    // 1) `Enum.MEMBER`：基名是 enum 类型。
    for (const candidate of [
      scope,
      ...this.importClosure(scope, MAX_IMPORT_DEPTH),
    ]) {
      const isEnum = candidate.info.decls.some(
        (decl) => decl.name === base && decl.kind === "enum"
      );
      if (!isEnum) {
        continue;
      }
      const enumMember = candidate.info.enumMembers.find(
        (item) => item.enumName === base && item.name === member
      );
      if (enumMember) {
        return [locationAt(candidate.file, enumMember.line - 1)];
      }
    }

    // 2) `obj.field`：从 var/参数/字段的声明类型推断 owner struct。
    const typeName = simpleTypeName(scope.info.varTypes.get(base) ?? "");
    if (isUserTypeName(typeName)) {
      // 2a) 变量类型是 enum：`var s: Status; s.IDLE`
      for (const candidate of [
        scope,
        ...this.importClosure(scope, MAX_IMPORT_DEPTH),
      ]) {
        const isEnum = candidate.info.decls.some(
          (decl) => decl.name === typeName && decl.kind === "enum"
        );
        if (!isEnum) {
          continue;
        }
        const enumMember = candidate.info.enumMembers.find(
          (item) => item.enumName === typeName && item.name === member
        );
        if (enumMember) {
          return [locationAt(candidate.file, enumMember.line - 1)];
        }
      }
      const struct = this.findStruct(scope, typeName);
      if (struct) {
        const location = this.fieldInChain(struct, member, new Set<string>());
        if (location) {
          return [location];
        }
      }
    }

    // 3) 兜底：同文件内同名字段 / 枚举成员（类型推不出时）。
    const fields = scope.info.fields
      .filter((field) => field.name === member)
      .map((field) => locationAt(scope.file, field.line - 1));
    if (fields.length > 0) {
      return dedupe(fields);
    }
    const members = scope.info.enumMembers
      .filter((item) => item.name === member)
      .map((item) => locationAt(scope.file, item.line - 1));
    return dedupe(members);
  }

  private addLocalLocations(
    locations: vscode.Location[],
    scope: Scope,
    word: string
  ): void {
    this.addDeclLocations(locations, scope, word);
    for (const field of scope.info.fields) {
      if (field.name === word) {
        locations.push(locationAt(scope.file, field.line - 1));
      }
    }
    for (const member of scope.info.enumMembers) {
      if (member.name === word) {
        locations.push(locationAt(scope.file, member.line - 1));
      }
    }
  }

  private addDeclLocations(
    locations: vscode.Location[],
    scope: Scope,
    word: string
  ): void {
    for (const decl of scope.info.decls) {
      if (decl.name === word) {
        locations.push(locationAt(scope.file, decl.line - 1));
      }
    }
  }

  // -------------------------------------------------------------------------
  // 结构体 / 字段
  // -------------------------------------------------------------------------

  private findStruct(scope: Scope, name: string): StructDef | undefined {
    const tryScope = (candidate: Scope): StructDef | undefined => {
      const decl = candidate.info.decls.find(
        (item) =>
          item.name === name &&
          (item.kind === "struct" || item.kind === "event")
      );
      if (!decl) {
        return undefined;
      }
      return {
        scope: candidate,
        name,
        kind: decl.kind as "struct" | "event",
      };
    };
    const local = tryScope(scope);
    if (local) {
      return local;
    }
    for (const imported of this.importClosure(scope, MAX_IMPORT_DEPTH)) {
      const hit = tryScope(imported);
      if (hit) {
        return hit;
      }
    }
    for (const decl of this.index.findDeclarations(name)) {
      if (decl.kind !== "struct" && decl.kind !== "event") {
        continue;
      }
      const fileScope = this.scopeForFile(decl.file);
      if (!fileScope) {
        continue;
      }
      const hit = tryScope(fileScope);
      if (hit) {
        return hit;
      }
    }
    return undefined;
  }

  private fieldInChain(
    def: StructDef,
    fieldName: string,
    seen: Set<string>
  ): vscode.Location | undefined {
    const key = `${fileKey(def.scope.file)}#${def.name}`;
    if (seen.has(key) || seen.size > MAX_BASE_DEPTH) {
      return undefined;
    }
    seen.add(key);
    const field = def.scope.info.fields.find(
      (item) => item.owner === def.name && item.name === fieldName
    );
    if (field) {
      return locationAt(def.scope.file, field.line - 1);
    }
    const base = def.scope.info.bases.get(def.name);
    if (!base) {
      return undefined;
    }
    const baseDef = this.findStruct(def.scope, base);
    return baseDef
      ? this.fieldInChain(baseDef, fieldName, seen)
      : undefined;
  }

  // -------------------------------------------------------------------------
  // import / 文件作用域
  // -------------------------------------------------------------------------

  private importAt(
    source: string,
    file: string,
    offset: number
  ): string | undefined {
    for (const imp of parseImports(source)) {
      if (offset < imp.start || offset > imp.end) {
        continue;
      }
      const statement = source.slice(imp.start, imp.end);
      const quote = statement.indexOf('"');
      let specStart: number;
      let specEnd: number;
      if (quote >= 0) {
        specStart = imp.start + quote + 1;
        specEnd = source.indexOf('"', specStart);
        if (specEnd < 0) {
          specEnd = imp.end;
        }
      } else {
        specStart = imp.start + "import".length;
        specEnd = imp.end - 1;
      }
      if (offset >= specStart && offset < specEnd) {
        return resolveImportSpec(imp.spec, file) ?? undefined;
      }
    }
    return undefined;
  }

  private documentScope(document: vscode.TextDocument): Scope {
    const cached = this.docCache.get(document);
    if (cached && cached.version === document.version) {
      return cached.scope;
    }
    const scope: Scope = {
      file: document.uri.fsPath,
      info: parseFileInfo(document.getText()),
    };
    this.docCache.set(document, { version: document.version, scope });
    return scope;
  }

  private scopeForFile(file: string): Scope | undefined {
    const absolute = path.resolve(file);
    const key = fileKey(absolute);
    let mtimeMs: number;
    try {
      mtimeMs = fs.statSync(absolute).mtimeMs;
    } catch {
      return undefined;
    }
    const cached = this.fileCache.get(key);
    if (cached && cached.mtimeMs === mtimeMs) {
      return cached.scope;
    }
    let text: string;
    try {
      text = fs.readFileSync(absolute, "utf8");
    } catch {
      return undefined;
    }
    const scope: Scope = { file: absolute, info: parseFileInfo(text) };
    this.fileCache.set(key, { mtimeMs, scope });
    return scope;
  }

  private importClosure(scope: Scope, maxDepth: number): Scope[] {
    const result: Scope[] = [];
    const seen = new Set<string>([fileKey(scope.file)]);
    let frontier: Scope[] = [scope];
    for (let depth = 0; depth < maxDepth && frontier.length > 0; depth += 1) {
      const next: Scope[] = [];
      for (const current of frontier) {
        for (const imp of current.info.imports) {
          const target = resolveImportSpec(imp.spec, current.file);
          if (!target) {
            continue;
          }
          const key = fileKey(target);
          if (seen.has(key)) {
            continue;
          }
          seen.add(key);
          const importedScope = this.scopeForFile(target);
          if (importedScope) {
            result.push(importedScope);
            next.push(importedScope);
          }
        }
      }
      frontier = next;
    }
    return result;
  }

  /** 清空文件缓存（工作区文件内容变化时由扩展调用）。 */
  clearCache(): void {
    this.fileCache.clear();
  }
}
