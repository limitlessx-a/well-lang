#!/usr/bin/env node
/*
 * 扩展激活 / 功能冒烟测试（纯 node + 自建 vscode mock，不启动 Electron）。
 *
 * 覆盖：
 *   1. activate() 注册命令与各 Provider，deactivate() 正常释放；
 *   2. 格式化 Provider：真实格式化一段 Well 代码，且幂等；
 *   3. 诊断链路（端到端）：调用真实 `wellc.py check --json`，在 mock 的
 *      DiagnosticCollection 中校验错误消息/行号/来源；
 *   4. 整理导入代码操作：排序/去重/移除未使用（含真实文件解析）；
 *   5. 自动导入：工作区索引找到其它文件的声明 → 代码操作 / 补全命令插入 import。
 *
 * 运行前置：`npm run compile`（生成 out/），然后 `node test/activation.test.js`。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const Module = require("module");

const EXT_ROOT = path.join(__dirname, "..");
const PROJECT_ROOT = path.join(EXT_ROOT, "..");
const REPO_ROOT = path.join(PROJECT_ROOT, "..");
const OUT_EXT = path.join(EXT_ROOT, "out", "extension.js");
// 用 build/tmp 而不是 test/tmp：test/tmp 是 imports.test.js 的临时目录（它会在结束时递归清理）。
const TMP = path.join(EXT_ROOT, "build", "tmp", "activation");

if (!fs.existsSync(OUT_EXT)) {
  console.error(`[activation.test] 缺少编译产物: ${OUT_EXT}`);
  console.error("请先运行: npm run compile");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// vscode mock
// ---------------------------------------------------------------------------

const diagStore = new Map(); // "fsPath" -> vscode.Diagnostic[]
const state = {
  commands: new Map(),
  formatProviders: [],
  codeActionProviders: [],
  completionProviders: [],
  definitionProviders: [],
  appliedEdits: [],
  outputChannels: [],
  diagnostics: diagStore,
  activeTextEditor: undefined,
  textDocuments: [],
};

class Position {
  constructor(line, character) {
    this.line = line;
    this.character = character;
  }
}

class Range {
  constructor(a, b, c, d) {
    if (typeof a === "number") {
      this.start = new Position(a, b);
      this.end = new Position(c, d);
    } else {
      this.start = a;
      this.end = b;
    }
  }
}

class Uri {
  constructor(fsPath) {
    this.fsPath = path.resolve(fsPath);
    this.path = this.fsPath.split(path.sep).join("/");
  }
  toString() {
    return "file://" + this.path;
  }
  static file(fsPath) {
    return new Uri(fsPath);
  }
  static parse(value) {
    const plain = value.replace(/^file:\/\//, "");
    return new Uri(plain.split("/").join(path.sep));
  }
}

class Diagnostic {
  constructor(range, message, severity) {
    this.range = range;
    this.message = message;
    this.severity = severity;
    this.source = undefined;
    this.code = undefined;
  }
}

class TextEdit {
  constructor(range, newText) {
    this.range = range;
    this.newText = newText;
  }
  static replace(range, newText) {
    return new TextEdit(range, newText);
  }
}

class WorkspaceEdit {
  constructor() {
    this.edits = [];
  }
  replace(uri, range, newText) {
    this.edits.push({ uri, range, newText });
  }
}

class CompletionItem {
  constructor(label, kind) {
    this.label = label;
    this.kind = kind;
    this.detail = undefined;
    this.documentation = undefined;
    this.sortText = undefined;
    this.filterText = undefined;
    this.command = undefined;
    this.additionalTextEdits = undefined;
  }
}

class MarkdownString {
  constructor(value) {
    this.value = value;
  }
}

class CodeAction {
  constructor(title, kind) {
    this.title = title;
    this.kind = kind;
    this.edit = undefined;
    this.isPreferred = undefined;
  }
}

class Location {
  constructor(uri, rangeOrPosition) {
    this.uri = uri;
    this.range =
      rangeOrPosition instanceof Range
        ? rangeOrPosition
        : new Range(rangeOrPosition, rangeOrPosition);
  }
}

class CancellationTokenSource {
  constructor() {
    this._requested = false;
    const self = this;
    this.token = {
      get isCancellationRequested() {
        return self._requested;
      },
      onCancellationRequested() {
        return { dispose() {} };
      },
    };
  }
  cancel() {
    this._requested = true;
  }
  dispose() {}
}

class CancellationError extends Error {}

const codeActionKind = {
  QuickFix: { value: "quickfix" },
  Refactor: { value: "refactor" },
  SourceOrganizeImports: { value: "source.organizeImports" },
  Source: { value: "source" },
};

const completionItemKind = {
  Text: 0,
  Method: 1,
  Function: 2,
  Constructor: 3,
  Field: 4,
  Variable: 5,
  Class: 6,
  Interface: 7,
  Module: 8,
  Property: 9,
  Unit: 10,
  Value: 11,
  Enum: 12,
  Keyword: 13,
  Snippet: 14,
  Color: 15,
  File: 16,
  Reference: 17,
  Folder: 18,
  EnumMember: 19,
  Constant: 20,
  Struct: 21,
  Event: 22,
  Operator: 23,
  TypeParameter: 24,
};

function disposable() {
  return { dispose() {} };
}

const configStore = {
  "well.enable": true,
  "well.pythonPath": process.platform === "win32" ? "python" : "python3",
  "well.compilerPath": "",
  "well.diagnostics.enable": true,
  "well.diagnostics.delayMs": 300,
  "well.format.enable": true,
  "well.imports.sort": "path",
  "well.imports.removeUnused": true,
  "well.index.exclude": [
    "**/node_modules/**",
    "**/build/**",
    "**/out/**",
    "**/.git/**",
    "**/.svn/**",
  ],
};

function configFor(section) {
  const prefix = section ? `${section}.` : "";
  return {
    get(key, fallback) {
      const full = prefix + key;
      return Object.prototype.hasOwnProperty.call(configStore, full)
        ? configStore[full]
        : fallback;
    },
    has() {
      return true;
    },
    update() {
      return Promise.resolve();
    },
  };
}

function walkWeFiles(root) {
  const found = [];
  const skip = new Set(["node_modules", "build", "out", ".git", ".svn"]);
  const visit = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.isDirectory()) {
        if (!skip.has(entry.name)) {
          visit(path.join(dir, entry.name));
        }
      } else if (entry.isFile() && entry.name.endsWith(".we")) {
        found.push(Uri.file(path.join(dir, entry.name)));
      }
    }
  };
  visit(root);
  return found;
}

const watcher = {
  onDidCreate() {
    return disposable();
  },
  onDidChange() {
    return disposable();
  },
  onDidDelete() {
    return disposable();
  },
  dispose() {},
};

const vscodeMock = {
  Position,
  Range,
  Uri,
  Diagnostic,
  TextEdit,
  WorkspaceEdit,
  CompletionItem,
  MarkdownString,
  CodeAction,
  CodeActionKind: codeActionKind,
  Location,
  CompletionItemKind: completionItemKind,
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
  CancellationTokenSource,
  CancellationError,
  Disposable: class {
    dispose() {}
  },
  EventEmitter: class {
    constructor() {
      this.event = () => disposable();
    }
    fire() {}
    dispose() {}
  },
  window: {
    get activeTextEditor() {
      return state.activeTextEditor;
    },
    createOutputChannel(name) {
      const channel = {
        name,
        lines: [],
        appendLine(line) {
          this.lines.push(line);
        },
        append(line) {
          this.lines.push(line);
        },
        show() {},
        hide() {},
        clear() {},
        dispose() {},
      };
      state.outputChannels.push(channel);
      return channel;
    },
    showInformationMessage() {
      return Promise.resolve(undefined);
    },
    showWarningMessage() {
      return Promise.resolve(undefined);
    },
    showErrorMessage() {
      return Promise.resolve(undefined);
    },
    setStatusBarMessage() {
      return { dispose() {} };
    },
    showQuickPick() {
      return Promise.resolve(undefined);
    },
    showTextDocument() {
      return Promise.resolve(undefined);
    },
  },
  workspace: {
    get workspaceFolders() {
      return [
        {
          uri: Uri.file(REPO_ROOT),
          name: path.basename(REPO_ROOT),
          index: 0,
        },
      ];
    },
    get textDocuments() {
      return state.textDocuments;
    },
    getConfiguration: configFor,
    getWorkspaceFolder() {
      return { uri: Uri.file(REPO_ROOT), name: path.basename(REPO_ROOT), index: 0 };
    },
    findFiles() {
      return Promise.resolve(walkWeFiles(REPO_ROOT));
    },
    fs: {
      readFile(uri) {
        return fs.promises.readFile(uri.fsPath);
      },
    },
    createFileSystemWatcher() {
      return watcher;
    },
    onDidChangeWorkspaceFolders() {
      return disposable();
    },
    onDidChangeConfiguration() {
      return disposable();
    },
    onDidChangeTextDocument() {
      return disposable();
    },
    onDidSaveTextDocument() {
      return disposable();
    },
    onDidOpenTextDocument() {
      return disposable();
    },
    onDidCloseTextDocument() {
      return disposable();
    },
    applyEdit(edit) {
      state.appliedEdits.push(edit);
      return Promise.resolve(true);
    },
  },
  languages: {
    createDiagnosticCollection() {
      return {
        set(uri, diagnostics) {
          diagStore.set(uri.fsPath, diagnostics);
        },
        delete(uri) {
          diagStore.delete(uri.fsPath);
        },
        clear() {
          diagStore.clear();
        },
        dispose() {
          diagStore.clear();
        },
      };
    },
    registerDocumentFormattingEditProvider(_language, provider) {
      state.formatProviders.push(provider);
      return disposable();
    },
    registerCodeActionsProvider(_language, provider) {
      state.codeActionProviders.push(provider);
      return disposable();
    },
    registerCompletionItemProvider(_language, provider) {
      state.completionProviders.push(provider);
      return disposable();
    },
    registerDefinitionProvider(_language, provider) {
      state.definitionProviders.push(provider);
      return disposable();
    },
    registerRangeFormattingEditProvider() {
      return disposable();
    },
  },
  commands: {
    registerCommand(id, callback) {
      state.commands.set(id, callback);
      return disposable();
    },
    executeCommand() {
      return Promise.resolve(undefined);
    },
  },
};

const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") {
    return vscodeMock;
  }
  return originalLoad.call(this, request, parent, isMain);
};

// ---------------------------------------------------------------------------
// 测试框架
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
const failures = [];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message || "assertion failed");
  }
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      `${message || "值不相等"}\n--- expected ---\n${JSON.stringify(
        expected
      )}\n--- actual ---\n${JSON.stringify(actual)}`
    );
  }
}

function test(name, fn) {
  try {
    const result = fn();
    return Promise.resolve(result)
      .then(() => {
        passed += 1;
        console.log(`ok - ${name}`);
      })
      .catch((err) => {
        failed += 1;
        failures.push({ name, err });
        console.error(`FAIL - ${name}`);
        console.error(err && err.stack ? err.stack : String(err));
      });
  } catch (err) {
    failed += 1;
    failures.push({ name, err });
    console.error(`FAIL - ${name}`);
    console.error(err && err.stack ? err.stack : String(err));
    return Promise.resolve();
  }
}

function waitFor(condition, timeoutMs = 20000, label = "condition") {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      let value;
      try {
        value = condition();
      } catch (err) {
        reject(err);
        return;
      }
      if (value) {
        resolve(value);
        return;
      }
      if (Date.now() - started > timeoutMs) {
        reject(new Error(`等待超时（${label}）`));
        return;
      }
      setTimeout(tick, 50);
    };
    tick();
  });
}

function makeDocument(text, fsPath, languageId = "well") {
  const lineStarts = [0];
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "\n") {
      lineStarts.push(i + 1);
    }
  }
  const document = {
    uri: Uri.file(fsPath),
    languageId,
    isUntitled: false,
    version: 1,
    lineCount: lineStarts.length,
    getText(range) {
      if (range) {
        return text.slice(
          document.offsetAt(range.start),
          document.offsetAt(range.end)
        );
      }
      return text;
    },
    positionAt(offset) {
      let line = 0;
      while (line + 1 < lineStarts.length && lineStarts[line + 1] <= offset) {
        line += 1;
      }
      return new Position(line, offset - lineStarts[line]);
    },
    offsetAt(position) {
      const start = lineStarts[Math.min(position.line, lineStarts.length - 1)];
      return start + position.character;
    },
    getWordRangeAtPosition(position, regex) {
      const lineStart = lineStarts[Math.min(position.line, lineStarts.length - 1)];
      const lineEnd =
        position.line + 1 < lineStarts.length
          ? lineStarts[position.line + 1] - 1
          : text.length;
      const lineText = text.slice(lineStart, lineEnd);
      const re = new RegExp(regex.source, "g");
      let match;
      while ((match = re.exec(lineText)) !== null) {
        const start = match.index;
        const end = start + match[0].length;
        if (position.character >= start && position.character <= end) {
          return new Range(
            new Position(position.line, start),
            new Position(position.line, end)
          );
        }
        if (match[0].length === 0) {
          re.lastIndex += 1;
        }
      }
      return undefined;
    },
  };
  return document;
}

function applyEdits(source, document, edits) {
  // 仅用单编辑（singleEdit 输出的是一段替换），按文档坐标换算 offset。
  let text = source;
  const sorted = edits
    .map((edit) => edit)
    .sort((a, b) => document.offsetAt(b.range.start) - document.offsetAt(a.range.start));
  for (const edit of sorted) {
    const start = document.offsetAt(edit.range.start);
    const end = document.offsetAt(edit.range.end);
    text = text.slice(0, start) + edit.newText + text.slice(end);
  }
  return text;
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

async function main() {
  fs.mkdirSync(path.join(TMP, "lib"), { recursive: true });
  fs.writeFileSync(
    path.join(TMP, "lib", "a.we"),
    "module demo.lib.a;\n\nstruct AVo {\n    x: int;\n}\n",
    "utf8"
  );
  fs.writeFileSync(
    path.join(TMP, "lib", "b.we"),
    "module demo.lib.b;\n\nstruct BVo {\n    y: int;\n}\n",
    "utf8"
  );

  const extension = require(OUT_EXT);
  const context = { subscriptions: [], extensionUri: Uri.file(EXT_ROOT) };
  extension.activate(context);
  state.subscriptions = context.subscriptions;

  await test("activate：注册命令与各 Provider", () => {
    for (const id of [
      "well.checkDocument",
      "well.formatDocument",
      "well.organizeImports",
      "well.importSymbol",
      "well.showCompilerOutput",
      "well.completeImport",
    ]) {
      assert(state.commands.has(id), `缺少命令 ${id}`);
    }
    assertEqual(state.formatProviders.length, 1, "应注册 1 个格式化 Provider");
    assertEqual(state.codeActionProviders.length, 1, "应注册 1 个代码操作 Provider");
    assertEqual(state.completionProviders.length, 1, "应注册 1 个补全 Provider");
    assertEqual(state.definitionProviders.length, 1, "应注册 1 个转到定义 Provider");
    assertEqual(state.outputChannels.length, 1, "应创建 1 个输出通道");
  });

  await test("格式化 Provider：输出格式正确且幂等", () => {
    const provider = state.formatProviders[0];
    const source = "module demo.app;\nstruct A { x: int; }\n";
    const document = makeDocument(
      source,
      path.join(TMP, "lib", "a.we")
    );
    const edits = provider.provideDocumentFormattingEdits(
      document,
      { tabSize: 4, insertSpaces: true },
      new CancellationTokenSource().token
    );
    assertEqual(edits.length, 1, "应返回 1 个 TextEdit");
    const formatted = edits[0].newText;
    assert(formatted.endsWith("\n"), "格式化结果应以换行结尾");
    assert(formatted.includes("\n    x: int;\n}"), "字段应缩进为 4 空格");
    const second = provider.provideDocumentFormattingEdits(
      makeDocument(formatted, path.join(TMP, "lib", "a.we")),
      { tabSize: 4, insertSpaces: true },
      new CancellationTokenSource().token
    );
    assertEqual(second.length, 0, "格式化应幂等（第二次无编辑）");
  });

  await test("诊断链路：真实 wellc check 发布错误诊断", async () => {
    const badFile = path.join(
      PROJECT_ROOT,
      "compiler",
      "tests",
      "cases",
      "08_compile_error",
      "main.we"
    );
    assert(fs.existsSync(badFile), `测试文件不存在: ${badFile}`);
    const source = fs.readFileSync(badFile, "utf8");
    const document = makeDocument(source, badFile);
    state.activeTextEditor = { document };
    diagStore.clear();
    const checkCommand = state.commands.get("well.checkDocument");
    await checkCommand();
    const diagnostics = await waitFor(
      () => {
        const value = diagStore.get(badFile);
        return value && value.length > 0 ? value : undefined;
      },
      25000,
      "checkCommand 诊断"
    );
    const joined = diagnostics.map((d) => d.message).join("\n");
    assert(joined.includes("MissingController"), `诊断消息应包含符号名: ${joined}`);
    const first = diagnostics[0];
    assertEqual(first.source, "wellc", "诊断 source 应为 wellc");
    assertEqual(first.severity, 0, "诊断 severity 应为 Error");
    assertEqual(first.range.start.line, 2, "应定位到第 3 行（0-based 2）");
  });

  await test("整理导入：排序 / 去重 / 移除未使用", () => {
    const mainFile = path.join(TMP, "main.we");
    fs.mkdirSync(path.dirname(mainFile), { recursive: true });
    const source = [
      "module demo.main;",
      "",
      'import "lib/b";',
      'import "lib/a";',
      'import "lib/a";',
      "",
      "struct HolderVo {",
      "    a: AVo;",
      "}",
      "",
    ].join("\n");
    fs.writeFileSync(mainFile, source, "utf8");
    const document = makeDocument(source, mainFile);
    const provider = state.codeActionProviders[0];
    const position = new Position(6, 0);
    const actions = provider.provideCodeActions(
      document,
      new Range(position, position),
      { only: undefined, triggerKind: 1, diagnostics: [] },
      new CancellationTokenSource().token
    );
    const action = actions.find((a) => a.kind === codeActionKind.SourceOrganizeImports);
    assert(action, "应提供整理导入代码操作");
    assert(action.edit.edits.length > 0, "整理导入应包含编辑");
    // 应用编辑（可能只是删除区间，newText 为空），再断言最终文本。
    const applied = applyEdits(source, document, action.edit.edits);
    assert(
      applied.indexOf('import "lib/a";') >= 0,
      `应保留被使用的导入: ${applied}`
    );
    assert(
      applied.indexOf('import "lib/a";') <
        applied.indexOf('import "lib/b";') ||
        applied.indexOf('import "lib/b";') < 0,
      `导入应排序: ${applied}`
    );
    assert(
      (applied.match(/import "lib\/a";/g) || []).length === 1,
      `重复导入应去重: ${applied}`
    );
    assert(!applied.includes('import "lib/b";'), `未使用导入应移除: ${applied}`);
  });

  await test("自动导入：代码操作 / 补全命令插入 import", async () => {
    const mainFile = path.join(TMP, "auto", "main.we");
    fs.mkdirSync(path.dirname(mainFile), { recursive: true });
    const source = "module demo.auto;\n\nstruct HolderVo {\n    item: ParsedReq;\n}\n";
    fs.writeFileSync(mainFile, source, "utf8");
    const document = makeDocument(source, mainFile);
    state.textDocuments = [document];
    state.activeTextEditor = { document };

    const targetFile = path.join(
      PROJECT_ROOT,
      "compiler",
      "examples",
      "lib",
      "http_parser.we"
    );
    assert(fs.existsSync(targetFile), `目标声明文件不存在: ${targetFile}`);

    // 等索引完成（findFiles 异步读取整个仓库的 .we）。
    let actions = [];
    try {
      await waitFor(
        () => {
          const line = source.split("\n")[3];
          const col = line.indexOf("ParsedReq") + 2;
          actions = state.codeActionProviders[0].provideCodeActions(
            document,
            new Range(new Position(3, col), new Position(3, col)),
            { only: undefined, triggerKind: 1, diagnostics: [] },
            new CancellationTokenSource().token
          );
          return actions.some((a) => a.title.includes("导入 'ParsedReq'"));
        },
        30000,
        "索引包含 ParsedReq 并给出导入代码操作"
      );
    } catch (err) {
      console.error(
        "[activation.test] 自动导入超时，当前代码操作:",
        actions.map((a) => a.title)
      );
      console.error(
        "[activation.test] 当前词:",
        document.getText().split("\n")[3]
      );
      throw err;
    }
    const action = actions.find((a) => a.title.includes("导入 'ParsedReq'"));
    assert(action.edit && action.edit.edits.length > 0, "自动导入代码操作应带编辑");
    const editText = action.edit.edits.map((edit) => edit.newText).join("");
    assert(editText.includes("import \""), `编辑应插入 import: ${editText}`);
    assert(editText.includes("http_parser.we"), `import 应指向 http_parser.we: ${editText}`);

    // 补全 → 接受后执行 well.completeImport 命令
    const provider = state.completionProviders[0];
    const line = source.split("\n")[3];
    const col = line.indexOf("ParsedReq") + 3;
    const items = provider.provideCompletionItems(
      document,
      new Position(3, col),
      new CancellationTokenSource().token,
      { triggerKind: 1, triggerCharacter: undefined }
    );
    const item = items.find((candidate) => candidate.label === "ParsedReq");
    assert(item, "补全列表应包含 ParsedReq");
    assert(item.command && item.command.command === "well.completeImport", "补全项应携带自动导入命令");
    state.appliedEdits.length = 0;
    const completeImport = state.commands.get("well.completeImport");
    await completeImport({ uri: document.uri.toString(), spec: item.command.arguments[0].spec });
    assert(state.appliedEdits.length > 0, "completeImport 应应用 WorkspaceEdit");
    const inserted = state.appliedEdits[0].edits
      .map((entry) => entry.newText)
      .join("");
    assert(inserted.includes("import \""), `应插入 import: ${inserted}`);
    assert(inserted.includes("http_parser.we"), `import 指向 http_parser.we: ${inserted}`);
  });

  await test("completion guard: no auto-import inside strings/comments", () => {
    const source =
      'module demo.guard;\n\nstruct GuardVo {\n    a: String = "Mod";\n    // Mod comment\n}\n';
    const file = path.join(TMP, "guard", "main.we");
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, source, "utf8");
    const document = makeDocument(source, file);
    const provider = state.completionProviders[0];
    const token = new CancellationTokenSource().token;
    const context = { triggerKind: 1, triggerCharacter: undefined };
    const inString = provider.provideCompletionItems(
      document,
      new Position(3, "    a: String = \"Mod\";".indexOf("Mod") + 2),
      token,
      context
    );
    assertEqual(inString.length, 0, "string should not offer completions");
    const inComment = provider.provideCompletionItems(
      document,
      new Position(4, "    // Mod comment".indexOf("Mod") + 2),
      token,
      context
    );
    assertEqual(inComment.length, 0, "comment should not offer completions");
  });

  await test("Ctrl+Click 转到定义（声明/类型/跨文件/import/字段/枚举）", () => {
    const dir = path.join(TMP, "def");
    fs.mkdirSync(dir, { recursive: true });
    const libPath = path.join(dir, "lib.we");
    const libText = [
      "module demo.lib;",
      "",
      "struct LibVo {",
      "    libField: int;",
      "}",
      "",
    ].join("\n");
    fs.writeFileSync(libPath, libText, "utf8");
    const mainPath = path.join(dir, "main.we");
    const mainText = [
      "module demo.def;",
      "",
      'import "lib.we";',
      "",
      "struct BaseVo {",
      "    baseField: int;",
      "}",
      "",
      "struct ItemVo extends BaseVo {",
      "    value: int;",
      '    label: String = "x";',
      "}",
      "",
      "enum Status {",
      "    IDLE;",
      "    RUNNING;",
      "}",
      "",
      "controller DefController {",
      "    run() {",
      "        var item: ItemVo = new ItemVo();",
      "        item.value = 1;",
      "        item.baseField = 2;",
      "        var status: Status = Status.IDLE;",
      "        status.RUNNING;",
      "        var lib: LibVo = new LibVo();",
      "        lib.libField = 3;",
      "    }",
      "}",
      "",
      "entry DefController;",
      "",
    ].join("\n");
    fs.writeFileSync(mainPath, mainText, "utf8");
    const document = makeDocument(mainText, mainPath);
    const provider = state.definitionProviders[0];
    assert(provider, "应存在转到定义 Provider");

    const positionAt = (needle, offset = 0) => {
      const index = mainText.indexOf(needle);
      assert(index >= 0, `main.we 找不到 ${needle}`);
      const absolute = index + offset;
      const before = mainText.slice(0, absolute);
      const line = before.split("\n").length - 1;
      const character = absolute - (before.lastIndexOf("\n") + 1);
      return new Position(line, character);
    };
    const expectDefinition = (position, expectedFile, expectedLineText) => {
      const result = provider.provideDefinition(document, position);
      assert(Array.isArray(result) && result.length > 0, "应返回定义位置");
      const first = result[0];
      assertEqual(first.uri.fsPath, expectedFile, "定义文件不匹配");
      const text =
        expectedFile === mainPath
          ? mainText
          : fs.readFileSync(expectedFile, "utf8");
      const targetLine = text.split("\n")[first.range.start.line] || "";
      assert(
        targetLine.includes(expectedLineText),
        `应跳到包含 "${expectedLineText}" 的行，实际: ${targetLine.trim()}`
      );
    };

    // 类型引用 / new / 基类 / entry
    expectDefinition(
      positionAt("var item: ItemVo", "var item: ".length),
      mainPath,
      "struct ItemVo"
    );
    expectDefinition(
      positionAt("new ItemVo", "new ".length),
      mainPath,
      "struct ItemVo"
    );
    expectDefinition(
      positionAt("item.value", "item.".length),
      mainPath,
      "value: int"
    );
    expectDefinition(
      positionAt("item.baseField", "item.".length),
      mainPath,
      "baseField: int"
    );
    expectDefinition(
      positionAt("status.RUNNING", "status.".length),
      mainPath,
      "RUNNING;"
    );
    // 跨文件（import 的声明 / 字段）
    expectDefinition(
      positionAt("var lib: LibVo", "var lib: ".length),
      libPath,
      "struct LibVo"
    );
    expectDefinition(
      positionAt("lib.libField", "lib.".length),
      libPath,
      "libField: int"
    );
    // import 字符串跳被导入文件
    expectDefinition(
      positionAt('import "lib.we"', 'import "'.length),
      libPath,
      "module demo.lib;"
    );
    // entry 引用 controller
    expectDefinition(
      positionAt("entry DefController", "entry ".length),
      mainPath,
      "controller DefController"
    );
  });

  await test("deactivate：释放订阅与输出通道", () => {
    const before = context.subscriptions.length;
    assert(before > 0, "应有订阅项");
    extension.deactivate();
    for (const subscription of context.subscriptions) {
      if (subscription && typeof subscription.dispose === "function") {
        subscription.dispose();
      }
    }
  });
}

main()
  .then(() => {
    console.log("");
    console.log("----------------------------------------");
    if (failed === 0) {
      console.log(`PASSED: ${passed} 个场景通过`);
      process.exit(0);
    } else {
      console.log(`FAILED: ${passed} 通过 / ${failed} 失败`);
      for (const failure of failures) {
        console.log(`  - ${failure.name}`);
      }
      process.exit(1);
    }
  })
  .catch((err) => {
    console.error("[activation.test] 未捕获异常", err);
    process.exit(1);
  });
