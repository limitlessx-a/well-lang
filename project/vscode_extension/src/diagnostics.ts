/**
 * Diagnostics provider: runs `wellc check --json` for the active Well document
 * (debounced while typing) and publishes the results to the Problems panel.
 */
import * as vscode from "vscode";
import {
  CompilerManager,
  CompilerUnavailableError,
  WellDiagnostic,
} from "./compiler";

function diagnosticsEnabled(): boolean {
  const cfg = vscode.workspace.getConfiguration("well");
  return cfg.get<boolean>("enable", true) && cfg.get<boolean>("diagnostics.enable", true);
}

function checkDelayMs(): number {
  const cfg = vscode.workspace.getConfiguration("well");
  const value = Number(cfg.get<number>("diagnostics.delayMs", 400));
  if (!Number.isFinite(value)) {
    return 400;
  }
  return Math.min(5000, Math.max(100, Math.trunc(value)));
}

export class DiagnosticsManager implements vscode.Disposable {
  private readonly collection =
    vscode.languages.createDiagnosticCollection("well");
  private readonly timers = new Map<string, NodeJS.Timeout>();
  private readonly tokens = new Map<string, vscode.CancellationTokenSource>();
  /** owner document URI -> diagnostic URIs produced by its last check */
  private readonly produced = new Map<string, Set<string>>();
  private disabled = false;

  constructor(
    private readonly compiler: CompilerManager,
    private readonly output: vscode.OutputChannel
  ) {}

  /** Called when settings change (enable/disable, compiler path, ...). */
  refreshAvailability(): void {
    const enabled = diagnosticsEnabled();
    if (!enabled) {
      this.disabled = true;
      this.clearAll();
      return;
    }
    this.disabled = false;
    this.compiler.resolve();
    this.compiler.clearCache();
  }

  /** Queue a debounced check for a document; latest call wins. */
  schedule(document: vscode.TextDocument, delayMs?: number): void {
    if (this.disabled || !diagnosticsEnabled() || !isWellDocument(document)) {
      return;
    }
    const key = document.uri.toString();
    const previous = this.timers.get(key);
    if (previous) {
      clearTimeout(previous);
    }
    const timer = setTimeout(() => {
      this.timers.delete(key);
      void this.check(document);
    }, delayMs ?? checkDelayMs());
    this.timers.set(key, timer);
  }

  /** Run a check immediately (on save / open / explicit command). */
  checkNow(document: vscode.TextDocument): Promise<void> {
    if (!diagnosticsEnabled() || !isWellDocument(document)) {
      return Promise.resolve();
    }
    this.disabled = false;
    return this.check(document);
  }

  /** Check every visible Well document (used on activation). */
  checkVisibleDocuments(): void {
    for (const document of vscode.workspace.textDocuments) {
      if (document.isUntitled || !isWellDocument(document)) {
        continue;
      }
      void this.checkNow(document);
    }
  }

  private async check(document: vscode.TextDocument): Promise<void> {
    const key = document.uri.toString();
    const file = document.uri.fsPath;
    if (!file) {
      return;
    }
    const previousTimer = this.timers.get(key);
    if (previousTimer) {
      clearTimeout(previousTimer);
      this.timers.delete(key);
    }
    this.tokens.get(key)?.cancel();
    this.tokens.get(key)?.dispose();
    const cancellation = new vscode.CancellationTokenSource();
    this.tokens.set(key, cancellation);
    try {
      const result = await this.compiler.checkFile(file, cancellation.token);
      if (cancellation.token.isCancellationRequested) {
        return;
      }
      this.publish(key, result.diagnostics);
    } catch (err) {
      if (err instanceof vscode.CancellationError) {
        return;
      }
      if (err instanceof CompilerUnavailableError) {
        this.publish(key, []);
        if (this.compiler.shouldNotifyUnavailable()) {
          this.compiler.markUnavailableNotified();
          void vscode.window
            .showWarningMessage(`Well: ${err.message}`, "打开设置")
            .then((choice) => {
              if (choice === "打开设置") {
                void vscode.commands.executeCommand(
                  "workbench.action.openSettings",
                  "well.compilerPath"
                );
              }
            });
        }
        return;
      }
      const message = err instanceof Error ? err.message : String(err);
      this.output.appendLine(`[diagnostics] ${message}`);
      this.publish(key, []);
    } finally {
      if (this.tokens.get(key) === cancellation) {
        this.tokens.delete(key);
      }
    }
  }

  private publish(ownerUri: string, diagnostics: WellDiagnostic[]): void {
    const grouped = new Map<string, vscode.Diagnostic[]>();
    for (const d of diagnostics) {
      const uri = vscode.Uri.file(d.file).toString();
      const list = grouped.get(uri) ?? [];
      list.push(toVsDiagnostic(d));
      grouped.set(uri, list);
    }
    const fresh = new Set(grouped.keys());
    const previous = this.produced.get(ownerUri) ?? new Set<string>();
    for (const uri of previous) {
      if (fresh.has(uri) || isProducedByOther(this.produced, ownerUri, uri)) {
        continue;
      }
      this.collection.delete(vscode.Uri.parse(uri));
    }
    for (const [uri, list] of grouped) {
      this.collection.set(vscode.Uri.parse(uri), list);
    }
    if (fresh.size === 0) {
      this.collection.delete(vscode.Uri.parse(ownerUri));
      this.produced.delete(ownerUri);
    } else {
      this.produced.set(ownerUri, fresh);
    }
  }

  clearDocument(document: vscode.TextDocument): void {
    const key = document.uri.toString();
    const previous = this.produced.get(key);
    if (previous) {
      for (const uri of previous) {
        if (!isProducedByOther(this.produced, key, uri)) {
          this.collection.delete(vscode.Uri.parse(uri));
        }
      }
      this.produced.delete(key);
    }
    this.collection.delete(document.uri);
  }

  clearAll(): void {
    for (const timer of this.timers.values()) {
      clearTimeout(timer);
    }
    this.timers.clear();
    for (const token of this.tokens.values()) {
      token.cancel();
      token.dispose();
    }
    this.tokens.clear();
    this.produced.clear();
    this.collection.clear();
  }

  dispose(): void {
    this.clearAll();
    this.collection.dispose();
  }
}

function isProducedByOther(
  produced: Map<string, Set<string>>,
  ownerUri: string,
  diagnosticUri: string
): boolean {
  for (const [owner, uris] of produced) {
    if (owner !== ownerUri && uris.has(diagnosticUri)) {
      return true;
    }
  }
  return false;
}

function isWellDocument(document: vscode.TextDocument): boolean {
  return document.languageId === "well" || document.uri.path.endsWith(".we");
}

function toVsDiagnostic(d: WellDiagnostic): vscode.Diagnostic {
  const line = Math.max(0, d.line - 1);
  const col = Math.max(0, d.col - 1);
  let endLine = Math.max(0, d.endLine - 1);
  let endCol = Math.max(0, d.endCol - 1);
  if (endLine < line || (endLine === line && endCol <= col)) {
    endLine = line;
    endCol = col + 1;
  }
  const range = new vscode.Range(line, col, endLine, endCol);
  const severity =
    d.severity === "warning"
      ? vscode.DiagnosticSeverity.Warning
      : vscode.DiagnosticSeverity.Error;
  const diagnostic = new vscode.Diagnostic(range, d.message, severity);
  diagnostic.source = "wellc";
  if (d.code) {
    diagnostic.code = d.code;
  }
  return diagnostic;
}
