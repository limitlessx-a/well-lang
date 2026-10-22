/**
 * Auto-import completions: suggest top-level declarations from other `.we`
 * files in the workspace and add the matching `import "..."` statement when
 * the completion is accepted.
 */
import * as vscode from "vscode";
import { tokenizeWell } from "./formatter";
import { importSpecFor, parseDeclarations } from "./wellParse";
import { WorkspaceIndex } from "./workspaceIndex";
import {
  declKindLabel,
  declKindToCompletionKind,
  displayPath,
  isWellDocument,
  wellEnabled,
} from "./util";

interface Builtin {
  name: string;
  kind: "class" | "function" | "constant";
  detail: string;
}

const BUILTINS: Builtin[] = [
  { name: "Controller", kind: "class", detail: "根控制器类型" },
  { name: "Handler", kind: "class", detail: "根处理器类型" },
  { name: "Event", kind: "class", detail: "根事件类型" },
  { name: "Vo", kind: "class", detail: "根结构体类型" },
  { name: "Model", kind: "class", detail: "Model.get/peek/set/remove" },
  { name: "Cache", kind: "class", detail: "Cache.get/set/remove" },
  { name: "IdUtil", kind: "class", detail: "IdUtil.getId()" },
  { name: "Json", kind: "class", detail: "Json.parse/tryParse/stringify" },
  { name: "Http", kind: "class", detail: "Http.route/listen/respond/stop" },
  { name: "Ws", kind: "class", detail: "Ws.route/listen/send/close/stop" },
  { name: "Mysql", kind: "class", detail: "Mysql.connect/exec/queryJson/..." },
  { name: "Timer", kind: "class", detail: "Timer.once/loop/cancel" },
  { name: "File", kind: "class", detail: "File.read/write/exists/remove" },
  { name: "Host", kind: "class", detail: "Host.nowMs/env/log" },
  { name: "String", kind: "class", detail: "String.fromInt/fromLong/..." },
  { name: "Bytes", kind: "class", detail: "可字节缓冲" },
  { name: "Array", kind: "class", detail: "Array<T>" },
  { name: "Map", kind: "class", detail: "Map<T>" },
  { name: "Linked", kind: "class", detail: "Linked<T>" },
  { name: "print", kind: "function", detail: "打印参数" },
  { name: "on", kind: "function", detail: "订阅事件" },
  { name: "once", kind: "function", detail: "订阅一次" },
  { name: "off", kind: "function", detail: "取消订阅" },
  { name: "emit", kind: "function", detail: "派发事件" },
  { name: "start", kind: "function", detail: "启动子控制器" },
  { name: "stop", kind: "function", detail: "停止子控制器" },
  { name: "true", kind: "constant", detail: "布尔真" },
  { name: "false", kind: "constant", detail: "布尔假" },
  { name: "null", kind: "constant", detail: "空引用" },
  { name: "ALL", kind: "constant", detail: "订阅所有 channel" },
];

export class WellCompletionProvider implements vscode.CompletionItemProvider {
  constructor(private readonly index: WorkspaceIndex) {}

  provideCompletionItems(
    document: vscode.TextDocument,
    position: vscode.Position,
    _token: vscode.CancellationToken,
    _context: vscode.CompletionContext
  ): vscode.CompletionItem[] {
    if (!wellEnabled() || !isWellDocument(document)) {
      return [];
    }
    if (isInsideStringOrComment(document, position)) {
      return [];
    }
    const range = document.getWordRangeAtPosition(
      position,
      /[A-Za-z_][A-Za-z0-9_]*/
    );
    const prefix = range ? document.getText(range) : "";
    if (prefix.length < 2) {
      return [];
    }
    const local = new Set(
      parseDeclarations(document.getText()).map((decl) => decl.name)
    );
    const currentFile = document.uri.fsPath;
    const items: vscode.CompletionItem[] = [];
    for (const builtin of BUILTINS) {
      if (!builtin.name.startsWith(prefix) || local.has(builtin.name)) {
        continue;
      }
      const item = new vscode.CompletionItem(
        builtin.name,
        builtin.kind === "function"
          ? vscode.CompletionItemKind.Function
          : builtin.kind === "constant"
            ? vscode.CompletionItemKind.Constant
            : vscode.CompletionItemKind.Class
      );
      item.detail = `内置 · ${builtin.detail}`;
      item.sortText = `2${builtin.name}`;
      items.push(item);
    }
    for (const decl of this.index.declarations) {
      if (decl.file === currentFile || local.has(decl.name)) {
        continue;
      }
      if (!decl.name.startsWith(prefix)) {
        continue;
      }
      const item = new vscode.CompletionItem(
        decl.name,
        declKindToCompletionKind(decl.kind)
      );
      item.detail = `${declKindLabel(decl.kind)} · ${displayPath(
        document,
        decl.file
      )}`;
      item.documentation = new vscode.MarkdownString(
        `自动导入 **${decl.name}**（${declKindLabel(
          decl.kind
        )}）\n\n来自 \`${displayPath(document, decl.file)}:${decl.line}\``
      );
      item.sortText = `1${decl.name}`;
      item.filterText = decl.name;
      const spec = importSpecFor(document.uri.fsPath, decl.file);
      item.command = {
        command: "well.completeImport",
        title: "添加 import",
        arguments: [{ uri: document.uri.toString(), spec }],
      };
      items.push(item);
    }
    return items;
  }
}

/** 光标位于字符串或注释内时不做补全（避免把字符串内容当成符号建议）。 */
function isInsideStringOrComment(
  document: vscode.TextDocument,
  position: vscode.Position
): boolean {
  const offset = document.offsetAt(position);
  for (const token of tokenizeWell(document.getText())) {
    if (token.kind !== "string" && token.kind !== "comment") {
      continue;
    }
    const start = document.offsetAt(
      new vscode.Position(token.line - 1, token.col - 1)
    );
    const end = start + token.raw.length;
    if (offset >= start && offset <= end) {
      return true;
    }
  }
  return false;
}
