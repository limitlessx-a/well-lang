/**
 * wellc bridge: locate the Python compiler, run `wellc check --json`, and
 * surface the structured diagnostics used by the diagnostics provider.
 */
import * as cp from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

/** One diagnostic emitted by `wellc check --json` (1-based positions). */
export interface WellDiagnostic {
  file: string;
  line: number;
  col: number;
  endLine: number;
  endCol: number;
  severity: "error" | "warning";
  message: string;
  code?: string;
}

export interface CheckResult {
  ok: boolean;
  file: string;
  diagnostics: WellDiagnostic[];
}

export class CompilerUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CompilerUnavailableError";
  }
}

interface CacheEntry {
  key: string;
  result: CheckResult;
}

const CHECK_TIMEOUT_MS = 20000;
const COMPILER_RELATIVE_PATHS = [
  "compiler/wellc.py",
  "project/compiler/wellc.py",
  "wellc.py",
];

/** Resolve a user supplied path against the first workspace folder (or cwd). */
function resolveConfigPath(value: string): string {
  if (path.isAbsolute(value)) {
    return value;
  }
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (folder) {
    return path.join(folder, value);
  }
  return path.resolve(value);
}

/** Walk up from `start` looking for `compiler/wellc.py` (repo checkout layout). */
function findCompilerUpwards(start: string): string | undefined {
  let dir = path.resolve(start);
  for (let i = 0; i < 8; i += 1) {
    for (const rel of COMPILER_RELATIVE_PATHS) {
      const candidate = path.join(dir, rel);
      if (isFile(candidate)) {
        return candidate;
      }
    }
    const parent = path.dirname(dir);
    if (parent === dir) {
      break;
    }
    dir = parent;
  }
  return undefined;
}

function isFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

function firstJsonObject(text: string): string | undefined {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) {
    return undefined;
  }
  return text.slice(start, end + 1);
}

function asDiagnostic(raw: unknown): WellDiagnostic | undefined {
  if (!raw || typeof raw !== "object") {
    return undefined;
  }
  const d = raw as Record<string, unknown>;
  const file = typeof d.file === "string" ? d.file : undefined;
  const message = typeof d.message === "string" ? d.message : undefined;
  if (!file || !message) {
    return undefined;
  }
  const line = Math.max(1, Number(d.line) || 1);
  const col = Math.max(1, Number(d.col) || 1);
  const endLine = Math.max(line, Number(d.endLine) || line);
  const endCol = Math.max(col + 1, Number(d.endCol) || col + 1);
  return {
    file,
    line,
    col,
    endLine,
    endCol,
    severity: d.severity === "warning" ? "warning" : "error",
    message,
    code: typeof d.code === "string" ? d.code : undefined,
  };
}

export class CompilerManager implements vscode.Disposable {
  private compilerPath: string | undefined;
  private pythonPath: string | undefined;
  private readonly cache = new Map<string, CacheEntry>();
  private readonly running = new Set<number>();
  private unavailableNotified = false;

  constructor(private readonly output: vscode.OutputChannel) {}

  /** Absolute path to wellc.py, or undefined when it cannot be located. */
  getCompilerPath(): string | undefined {
    return this.compilerPath;
  }

  getPythonPath(): string {
    return this.pythonPath ?? "python";
  }

  /** Re-read the settings and re-resolve the compiler / interpreter paths. */
  resolve(): void {
    const cfg = vscode.workspace.getConfiguration("well");
    const configuredCompiler = String(cfg.get<string>("compilerPath", "") ?? "").trim();
    let compiler: string | undefined;
    if (configuredCompiler) {
      compiler = resolveConfigPath(configuredCompiler);
      if (!isFile(compiler)) {
        compiler = undefined;
      }
    }
    if (!compiler) {
      const folders = vscode.workspace.workspaceFolders ?? [];
      for (const folder of folders) {
        for (const rel of COMPILER_RELATIVE_PATHS) {
          const candidate = path.join(folder.uri.fsPath, rel);
          if (isFile(candidate)) {
            compiler = candidate;
            break;
          }
        }
        if (compiler) {
          break;
        }
      }
    }
    if (!compiler) {
      const active = vscode.window.activeTextEditor?.document.uri.fsPath;
      if (active) {
        compiler = findCompilerUpwards(path.dirname(active));
      }
    }
    this.compilerPath = compiler;
    const configuredPython = String(cfg.get<string>("pythonPath", "python") ?? "").trim();
    this.pythonPath = configuredPython || "python";
    if (compiler) {
      this.output.appendLine(`[compiler] wellc.py = ${compiler}`);
    } else {
      this.output.appendLine("[compiler] wellc.py not found (set well.compilerPath)");
    }
  }

  markUnavailableNotified(): void {
    this.unavailableNotified = true;
  }

  shouldNotifyUnavailable(): boolean {
    return !this.unavailableNotified;
  }

  clearCache(): void {
    this.cache.clear();
  }

  /** Run `wellc check --json` for one file, with a small mtime/size cache. */
  async checkFile(
    file: string,
    token?: vscode.CancellationToken
  ): Promise<CheckResult> {
    this.resolve();
    const compiler = this.compilerPath;
    if (!compiler) {
      throw new CompilerUnavailableError(
        "未找到 wellc.py。请在工作区打开 Well 仓库，或设置 well.compilerPath。"
      );
    }
    const key = await fileCacheKey(file);
    if (key) {
      const cached = this.cache.get(file);
      if (cached && cached.key === key) {
        return cached.result;
      }
    }
    const args = [compiler, "check", "--json", file];
    this.output.appendLine(`[check] ${this.getPythonPath()} ${args.join(" ")}`);
    const result = await runProcess(
      this.getPythonPath(),
      args,
      path.dirname(compiler),
      CHECK_TIMEOUT_MS,
      token,
      (pid) => this.running.add(pid),
      (pid) => this.running.delete(pid)
    );
    if (result.killed) {
      throw new vscode.CancellationError();
    }
    const payload = firstJsonObject(result.stdout);
    if (!payload) {
      this.output.appendLine(`[check] unexpected output:\n${result.stdout}\n${result.stderr}`);
      throw new Error(
        `wellc check 输出无法解析（退出码 ${result.code}）：${(result.stderr || result.stdout).slice(0, 500)}`
      );
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(payload) as Record<string, unknown>;
    } catch (e) {
      this.output.appendLine(`[check] invalid JSON: ${String(e)}`);
      throw new Error(`wellc check 返回了非法 JSON：${String(e)}`);
    }
    const diagnostics = Array.isArray(parsed.diagnostics)
      ? parsed.diagnostics
          .map(asDiagnostic)
          .filter((d): d is WellDiagnostic => d !== undefined)
      : [];
    const checkResult: CheckResult = {
      ok: parsed.ok !== false,
      file,
      diagnostics,
    };
    if (key) {
      this.cache.set(file, { key, result: checkResult });
    }
    return checkResult;
  }

  /** Kill any compiler process that is still running (extension shutdown). */
  killAll(): void {
    for (const pid of this.running) {
      try {
        process.kill(pid);
      } catch {
        /* already gone */
      }
    }
    this.running.clear();
  }

  dispose(): void {
    this.killAll();
    this.cache.clear();
  }
}

async function fileCacheKey(file: string): Promise<string | undefined> {
  try {
    const st = await fs.promises.stat(file);
    return `${st.mtimeMs}:${st.size}`;
  } catch {
    return undefined;
  }
}

interface ProcessResult {
  code: number | null;
  stdout: string;
  stderr: string;
  killed: boolean;
}

function runProcess(
  command: string,
  args: string[],
  cwd: string,
  timeoutMs: number,
  token: vscode.CancellationToken | undefined,
  onStart: (pid: number) => void,
  onEnd: (pid: number) => void
): Promise<ProcessResult> {
  return new Promise<ProcessResult>((resolve, reject) => {
    const child = cp.spawn(command, args, {
      cwd,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let killed = false;
    let settled = false;
    if (child.pid) {
      onStart(child.pid);
    }
    const finish = (result: ProcessResult) => {
      if (settled) {
        return;
      }
      settled = true;
      if (child.pid) {
        onEnd(child.pid);
      }
      resolve(result);
    };
    const fail = (err: Error) => {
      if (settled) {
        return;
      }
      settled = true;
      if (child.pid) {
        onEnd(child.pid);
      }
      reject(err);
    };
    const kill = () => {
      killed = true;
      child.kill();
    };
    const timer = setTimeout(kill, timeoutMs);
    child.stdout?.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr?.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.on("error", (err) => {
      clearTimeout(timer);
      fail(new CompilerUnavailableError(`无法启动 Python（${command}）：${err.message}`));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      finish({
        code,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
        killed,
      });
    });
    token?.onCancellationRequested(() => kill());
  });
}
