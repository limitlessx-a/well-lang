/**
 * Well (.we) language extension entry point.
 *
 * Features:
 *  - syntax highlighting (TextMate grammar, declared in package.json)
 *  - diagnostics (`wellc check --json`)
 *  - formatting (`formatWell`)
 *  - organize imports / auto-import (workspace symbol index)
 */
import * as vscode from "vscode";
import { formatWell } from "./formatter";
import { addImport, importSpecExists, organizeImports } from "./imports";
import { importSpecFor, parseDeclarations, parseImports } from "./wellParse";
import { IndexedDecl, WorkspaceIndex } from "./workspaceIndex";
import { CompilerManager } from "./compiler";
import { DiagnosticsManager } from "./diagnostics";
import { WellCompletionProvider } from "./completion";
import {
  declKindLabel,
  displayPath,
  importContext,
  isWellDocument,
  singleEdit,
  wellEnabled,
  wordAt,
} from "./util";

let outputChannel: vscode.OutputChannel | undefined;

function formatEnabled(): boolean {
  return (
    wellEnabled() &&
    vscode.workspace.getConfiguration("well").get<boolean>("format.enable", true)
  );
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Well");
  outputChannel = output;
  const index = new WorkspaceIndex();
  const compiler = new CompilerManager(output);
  const diagnostics = new DiagnosticsManager(compiler, output);

  compiler.resolve();
  void index.refresh().catch((err) => {
    output.appendLine(`[index] refresh failed: ${String(err)}`);
  });

  context.subscriptions.push(output, index, compiler, diagnostics);

  context.subscriptions.push(
    vscode.languages.registerDocumentFormattingEditProvider("well", {
      provideDocumentFormattingEdits(
        document: vscode.TextDocument,
        options: vscode.FormattingOptions
      ): vscode.TextEdit[] {
        if (!formatEnabled() || !isWellDocument(document)) {
          return [];
        }
        const source = document.getText();
        let formatted: string;
        try {
          formatted = formatWell(source, {
            tabSize: options.tabSize,
            insertSpaces: options.insertSpaces,
          });
        } catch (err) {
          output.appendLine(`[format] ${String(err)}`);
          return [];
        }
        if (formatted === source) {
          return [];
        }
        return [
          vscode.TextEdit.replace(
            new vscode.Range(document.positionAt(0), document.positionAt(source.length)),
            formatted
          ),
        ];
      },
    })
  );

  context.subscriptions.push(
    vscode.languages.registerCodeActionsProvider(
      "well",
      new WellCodeActionProvider(index),
      {
        providedCodeActionKinds: [
          vscode.CodeActionKind.SourceOrganizeImports,
          vscode.CodeActionKind.QuickFix,
        ],
      }
    )
  );

  context.subscriptions.push(
    vscode.languages.registerCompletionItemProvider(
      "well",
      new WellCompletionProvider(index),
      ".",
      '"'
    )
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument((event) => {
      if (isWellDocument(event.document)) {
        diagnostics.schedule(event.document);
      }
    }),
    vscode.workspace.onDidSaveTextDocument((document) => {
      if (isWellDocument(document)) {
        void diagnostics.checkNow(document);
      }
    }),
    vscode.workspace.onDidOpenTextDocument((document) => {
      if (isWellDocument(document)) {
        diagnostics.schedule(document, 300);
      }
    }),
    vscode.workspace.onDidCloseTextDocument((document) => {
      if (isWellDocument(document)) {
        diagnostics.clearDocument(document);
      }
    }),
    vscode.workspace.onDidChangeConfiguration((event) => {
      compiler.resolve();
      diagnostics.refreshAvailability();
      if (event.affectsConfiguration("well.index.exclude")) {
        void index.refresh();
      }
      const active = vscode.window.activeTextEditor?.document;
      if (active && isWellDocument(active)) {
        void diagnostics.checkNow(active);
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      void index.refresh();
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("well.checkDocument", async () => {
      const document = activeWellDocument();
      if (!document) {
        void vscode.window.showInformationMessage("Well: 请先打开一个 .we 文件。");
        return;
      }
      try {
        await diagnostics.checkNow(document);
        const active = vscode.window.activeTextEditor?.document;
        if (active && active.uri.toString() === document.uri.toString()) {
          output.show(true);
        }
      } catch (err) {
        showError(err);
      }
    }),

    vscode.commands.registerCommand("well.formatDocument", async () => {
      const document = activeWellDocument();
      if (!document) {
        return;
      }
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.document !== document) {
        return;
      }
      const options = editor.options;
      const formatted = formatWell(document.getText(), {
        tabSize: typeof options.tabSize === "number" ? options.tabSize : 4,
        insertSpaces: options.insertSpaces !== false,
      });
      if (formatted === document.getText()) {
        void vscode.window.showInformationMessage("Well: 文件已符合格式规范。");
        return;
      }
      const edit = new vscode.WorkspaceEdit();
      edit.replace(
        document.uri,
        new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)),
        formatted
      );
      await vscode.workspace.applyEdit(edit);
    }),

    vscode.commands.registerCommand("well.organizeImports", async () => {
      const document = activeWellDocument();
      if (!document) {
        return;
      }
      await organizeDocument(document, index);
    }),

    vscode.commands.registerCommand("well.importSymbol", async () => {
      const document = activeWellDocument();
      if (!document) {
        return;
      }
      await importSymbolAt(document, index);
    }),

    // Executed by auto-import completion items after the symbol is inserted.
    vscode.commands.registerCommand(
      "well.completeImport",
      async (arg: { uri?: unknown; spec?: unknown } | undefined) => {
        if (!arg || typeof arg.uri !== "string" || typeof arg.spec !== "string") {
          return;
        }
        const document = vscode.workspace.textDocuments.find(
          (doc) => doc.uri.toString() === arg.uri
        );
        if (!document || !isWellDocument(document)) {
          return;
        }
        await insertImport(document, index, arg.spec, true);
      }
    ),

    vscode.commands.registerCommand("well.showCompilerOutput", () => {
      output.show(true);
    })
  );

  diagnostics.refreshAvailability();
  diagnostics.checkVisibleDocuments();
}

export function deactivate(): void {
  outputChannel?.dispose();
  outputChannel = undefined;
}

function activeWellDocument(): vscode.TextDocument | undefined {
  const document = vscode.window.activeTextEditor?.document;
  if (!document || !isWellDocument(document) || !wellEnabled()) {
    return undefined;
  }
  return document;
}

function showError(err: unknown): void {
  const message = err instanceof Error ? err.message : String(err);
  void vscode.window.showErrorMessage(`Well: ${message}`);
}

function declaredNames(document: vscode.TextDocument): Set<string> {
  return new Set(parseDeclarations(document.getText()).map((decl) => decl.name));
}

/** Apply the organize-imports text edit to a document. */
async function organizeDocument(
  document: vscode.TextDocument,
  index: WorkspaceIndex
): Promise<void> {
  const source = document.getText();
  if (parseImports(source).length === 0) {
    void vscode.window.showInformationMessage("Well: 当前文件没有 import 语句。");
    return;
  }
  const updated = organizeImports(source, importContext(document, index));
  if (updated === source) {
    void vscode.window.showInformationMessage("Well: 导入已经整理好，无需修改。");
    return;
  }
  const edit = singleEdit(document, source, updated);
  if (!edit) {
    return;
  }
  const workspaceEdit = new vscode.WorkspaceEdit();
  workspaceEdit.replace(document.uri, edit.range, edit.newText);
  const applied = await vscode.workspace.applyEdit(workspaceEdit);
  if (applied) {
    void vscode.window.setStatusBarMessage("Well: 已整理导入", 3000);
  }
}

/** Insert `import "..."` for a symbol under the cursor. */
async function importSymbolAt(
  document: vscode.TextDocument,
  index: WorkspaceIndex
): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document !== document) {
    return;
  }
  const found = wordAt(document, editor.selection.start);
  if (!found) {
    void vscode.window.showInformationMessage("Well: 请把光标放在要导入的符号上。");
    return;
  }
  const local = declaredNames(document);
  if (local.has(found.word)) {
    void vscode.window.showInformationMessage(
      `Well: '${found.word}' 已在当前文件定义，无需导入。`
    );
    return;
  }
  const candidates = index
    .findDeclarations(found.word)
    .filter((decl) => decl.file !== document.uri.fsPath);
  if (candidates.length === 0) {
    void vscode.window.showWarningMessage(
      `Well: 工作区中未找到符号 '${found.word}' 的定义。`
    );
    return;
  }
  let chosen: IndexedDecl | undefined = candidates[0];
  if (candidates.length > 1) {
    const picked = await vscode.window.showQuickPick(
      candidates.map((decl) => ({
        label: decl.name,
        description: `${declKindLabel(decl.kind)} · ${displayPath(document, decl.file)}:${decl.line}`,
        decl,
      })),
      { placeHolder: `选择 '${found.word}' 的来源文件` }
    );
    if (!picked) {
      return;
    }
    chosen = picked.decl;
  }
  await insertImport(document, index, importSpecFor(document.uri.fsPath, chosen.file));
}

async function insertImport(
  document: vscode.TextDocument,
  index: WorkspaceIndex,
  spec: string,
  quiet = false
): Promise<void> {
  const source = document.getText();
  if (importSpecExists(source, spec)) {
    if (!quiet) {
      void vscode.window.showInformationMessage(`Well: 已经导入了 "${spec}"。`);
    }
    return;
  }
  const updated = addImport(source, importContext(document, index), spec);
  const edit = singleEdit(document, source, updated);
  if (!edit) {
    return;
  }
  const workspaceEdit = new vscode.WorkspaceEdit();
  workspaceEdit.replace(document.uri, edit.range, edit.newText);
  const applied = await vscode.workspace.applyEdit(workspaceEdit);
  if (applied) {
    void vscode.window.setStatusBarMessage(`Well: 已添加 import "${spec}"`, 3000);
  }
}

/** Lightbulb actions: organize imports and "import symbol from ...". */
class WellCodeActionProvider implements vscode.CodeActionProvider {
  constructor(private readonly index: WorkspaceIndex) {}

  provideCodeActions(
    document: vscode.TextDocument,
    range: vscode.Range | vscode.Selection
  ): vscode.CodeAction[] {
    if (!wellEnabled() || !isWellDocument(document)) {
      return [];
    }
    const source = document.getText();
    const actions: vscode.CodeAction[] = [];

    if (parseImports(source).length > 0) {
      const organized = organizeImports(
        source,
        importContext(document, this.index)
      );
      const edit = singleEdit(document, source, organized);
      if (edit) {
        const action = new vscode.CodeAction(
          "整理导入（排序 / 去重 / 移除未使用）",
          vscode.CodeActionKind.SourceOrganizeImports
        );
        const workspaceEdit = new vscode.WorkspaceEdit();
        workspaceEdit.replace(document.uri, edit.range, edit.newText);
        action.edit = workspaceEdit;
        actions.push(action);
      }
    }

    const found = wordAt(document, range.start);
    if (found) {
      const local = declaredNames(document);
      if (!local.has(found.word)) {
        const candidates = this.index
          .findDeclarations(found.word)
          .filter((decl) => decl.file !== document.uri.fsPath)
          .filter((decl) => {
            const spec = importSpecFor(document.uri.fsPath, decl.file);
            return !importSpecExists(source, spec);
          });
        for (const decl of candidates.slice(0, 3)) {
          const spec = importSpecFor(document.uri.fsPath, decl.file);
          const updated = addImport(source, importContext(document, this.index), spec);
          const edit = singleEdit(document, source, updated);
          if (!edit) {
            continue;
          }
          const action = new vscode.CodeAction(
            `导入 '${decl.name}'（${declKindLabel(decl.kind)}，来自 ${displayPath(
              document,
              decl.file
            )}）`,
            vscode.CodeActionKind.QuickFix
          );
          const workspaceEdit = new vscode.WorkspaceEdit();
          workspaceEdit.replace(document.uri, edit.range, edit.newText);
          action.edit = workspaceEdit;
          action.isPreferred = false;
          actions.push(action);
        }
      }
    }
    return actions;
  }
}
