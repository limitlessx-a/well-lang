/**
 * Small shared helpers used by the command handlers, code actions and the
 * auto-import completion provider.
 */
import * as path from "path";
import * as vscode from "vscode";
import { ImportContext } from "./imports";
import { DeclKind } from "./wellParse";
import { WorkspaceIndex } from "./workspaceIndex";

export function isWellDocument(document: vscode.TextDocument): boolean {
  return document.languageId === "well" || document.uri.path.endsWith(".we");
}

export function wellEnabled(): boolean {
  return vscode.workspace.getConfiguration("well").get<boolean>("enable", true);
}

/** Build the callback context needed by organizeImports/addImport. */
export function importContext(
  document: vscode.TextDocument,
  index: WorkspaceIndex
): ImportContext {
  return {
    file: document.uri.fsPath,
    declsOf: (absPath: string) => index.declsOfFile(absPath),
  };
}

/**
 * Compute a single text edit that turns `source` into `newSource`.
 * `organizeImports`/`addImport` only touch a contiguous region at the top, so a
 * prefix/suffix diff yields one clean edit that can be merged with other edits.
 */
export function singleEdit(
  document: vscode.TextDocument,
  source: string,
  newSource: string
): vscode.TextEdit | undefined {
  if (newSource === source) {
    return undefined;
  }
  const oldLen = source.length;
  const newLen = newSource.length;
  let prefix = 0;
  const maxPrefix = Math.min(oldLen, newLen);
  while (prefix < maxPrefix && source[prefix] === newSource[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  const maxSuffix = Math.min(oldLen, newLen) - prefix;
  while (
    suffix < maxSuffix &&
    source[oldLen - 1 - suffix] === newSource[newLen - 1 - suffix]
  ) {
    suffix += 1;
  }
  const start = document.positionAt(prefix);
  const end = document.positionAt(oldLen - suffix);
  const newText = newSource.slice(prefix, newLen - suffix);
  return vscode.TextEdit.replace(new vscode.Range(start, end), newText);
}

/** The identifier under the cursor/selection, if any. */
export function wordAt(
  document: vscode.TextDocument,
  position: vscode.Position
): { word: string; range: vscode.Range } | undefined {
  const range = document.getWordRangeAtPosition(position, /[A-Za-z_][A-Za-z0-9_]*/);
  if (!range) {
    return undefined;
  }
  return { word: document.getText(range), range };
}

export function declKindToCompletionKind(kind: DeclKind): vscode.CompletionItemKind {
  switch (kind) {
    case "struct":
    case "event":
      return vscode.CompletionItemKind.Struct;
    case "controller":
    case "handler":
    case "static":
      return vscode.CompletionItemKind.Class;
    case "enum":
      return vscode.CompletionItemKind.Enum;
    case "const":
      return vscode.CompletionItemKind.Constant;
    case "func":
      return vscode.CompletionItemKind.Function;
    case "namespace":
      return vscode.CompletionItemKind.Module;
    default:
      return vscode.CompletionItemKind.Variable;
  }
}

export function declKindLabel(kind: DeclKind): string {
  switch (kind) {
    case "struct":
      return "struct";
    case "event":
      return "event";
    case "controller":
      return "controller";
    case "handler":
      return "handler";
    case "static":
      return "static";
    case "enum":
      return "enum";
    case "const":
      return "const";
    case "func":
      return "func";
    case "namespace":
      return "namespace";
    default:
      return "decl";
  }
}

/** Short path for UI: relative to the workspace folder when possible. */
export function displayPath(document: vscode.TextDocument, file: string): string {
  if (!file) {
    return "";
  }
  const folder = vscode.workspace.getWorkspaceFolder(document.uri)?.uri.fsPath;
  if (folder) {
    const rel = path.relative(folder, file);
    if (rel && !rel.startsWith("..") && !path.isAbsolute(rel)) {
      return rel.split(path.sep).join("/");
    }
  }
  return file;
}
