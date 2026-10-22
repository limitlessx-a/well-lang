/**
 * Well (.we) 源文本的轻量扫描工具：顶层声明、import、module 以及 import 路径解析。
 *
 * 本文件是纯 TypeScript（只依赖 node 的 `fs`/`path`），不 import `vscode`，
 * 因此可以用 node 直接单测（见 test/imports.test.js）。
 *
 * `resolveImportSpec` 的语义必须与 `compiler/wellc/driver.py` 的
 * `_resolve_import` 完全一致（相对路径 / 带 .we / 点路径 / 目录 index.we /
 * 同目录 basename 回退）。若编译器端语义变化，这里必须同步修改。
 *
 * 扫描器只做“够用”的 token 化：跳过注释与字符串、记录 1-based 行号与 offset；
 * 括号深度只统计 `{` / `}`（Well 的顶层声明都以花括号结尾）。
 */

import * as fs from "fs";
import * as path from "path";

// ---------------------------------------------------------------------------
// 公共类型
// ---------------------------------------------------------------------------

export type DeclKind =
  | "struct"
  | "event"
  | "controller"
  | "handler"
  | "func"
  | "namespace"
  | "static"
  | "enum"
  | "const";

export interface DeclInfo {
  name: string;
  kind: DeclKind;
  /** 1-based 行号（声明关键字所在行）。 */
  line: number;
}

export interface ImportInfo {
  spec: string;
  /** 1-based 行号（import 关键字所在行）。 */
  line: number;
  /** 源文本 offset，指向 `import` 关键字首字符。 */
  start: number;
  /** 源文本 offset，指向分号之后（end 不含）。 */
  end: number;
}

export interface ModuleInfo {
  name: string;
  /** 1-based 行号。 */
  line: number;
  /** 源文本 offset，指向 `module` 关键字首字符。 */
  start: number;
  /** 源文本 offset，指向分号之后（end 不含）。 */
  end: number;
}

// ---------------------------------------------------------------------------
// token 扫描（internal + 供 definitions 复用）
// ---------------------------------------------------------------------------

export type WellScanTokenKind = "word" | "string" | "punct" | "number";

export interface WellScanToken {
  kind: WellScanTokenKind;
  /** word: 标识符/关键字原文；string: 反转义后的语义值；punct: 单字符。 */
  value: string;
  /** 1-based 行号。 */
  line: number;
  start: number;
  end: number;
}

const ID_START_RE = /[\p{L}_]/u;
const ID_CONTINUE_RE = /[\p{L}\p{N}_]/u;
const BOM = "\uFEFF";

/** 与 compiler/wellc/lexer.py 的字符串转义映射保持一致。 */
const ESCAPES: Record<string, string> = {
  n: "\n",
  t: "\t",
  r: "\r",
  "\\": "\\",
  '"': '"',
  "0": "\0",
};

function isDigit(c: string): boolean {
  return c >= "0" && c <= "9";
}

function isHexDigit(c: string): boolean {
  return isDigit(c) || (c >= "a" && c <= "f") || (c >= "A" && c <= "F");
}

function isSpace(c: string): boolean {
  return (
    c === " " ||
    c === "\t" ||
    c === "\r" ||
    c === "\n" ||
    c === "\f" ||
    c === "\v" ||
    c === BOM ||
    c === "\u00a0"
  );
}

/**
 * 把源码扫描成 token 序列（跳过注释与空白，保留字符串的语义值）。
 * 永不抛异常：未闭合的字符串/块注释按“一直延续到文件末尾”处理。
 */
export function scanWellTokens(source: string): WellScanToken[] {
  const tokens: WellScanToken[] = [];
  const n = source.length;
  let i = 0;
  let line = 1;

  while (i < n) {
    const c = source[i];

    if (c === "\n") {
      line += 1;
      i += 1;
      continue;
    }
    if (isSpace(c)) {
      i += 1;
      continue;
    }

    // 行注释
    if (c === "/" && source[i + 1] === "/") {
      i += 2;
      while (i < n && source[i] !== "\n") {
        i += 1;
      }
      continue;
    }

    // 块注释（不嵌套，与编译器 lexer 一致）
    if (c === "/" && source[i + 1] === "*") {
      i += 2;
      while (i < n && !(source[i] === "*" && source[i + 1] === "/")) {
        if (source[i] === "\n") {
          line += 1;
        }
        i += 1;
      }
      if (i < n) {
        i += 2;
      }
      continue;
    }

    // 字符串字面量
    if (c === '"') {
      const start = i;
      const startLine = line;
      i += 1;
      let value = "";
      while (i < n && source[i] !== '"') {
        if (source[i] === "\\" && i + 1 < n) {
          const esc = source[i + 1];
          value += ESCAPES[esc] ?? esc;
          i += 2;
          continue;
        }
        if (source[i] === "\n") {
          line += 1;
        }
        value += source[i];
        i += 1;
      }
      if (i < n) {
        i += 1; // 结束引号
      }
      tokens.push({ kind: "string", value, line: startLine, start, end: i });
      continue;
    }

    // 标识符 / 关键字
    if (ID_START_RE.test(c)) {
      const start = i;
      const startLine = line;
      i += 1;
      while (i < n && ID_CONTINUE_RE.test(source[i])) {
        i += 1;
      }
      tokens.push({
        kind: "word",
        value: source.slice(start, i),
        line: startLine,
        start,
        end: i,
      });
      continue;
    }

    // 数字（十进制/十六进制/小数/科学计数法），与 lexer 大致一致
    if (isDigit(c) || (c === "." && i + 1 < n && isDigit(source[i + 1]))) {
      const start = i;
      if (c === "0" && (source[i + 1] === "x" || source[i + 1] === "X")) {
        i += 2;
        while (i < n && isHexDigit(source[i])) {
          i += 1;
        }
      } else {
        while (i < n && isDigit(source[i])) {
          i += 1;
        }
        if (i < n && source[i] === ".") {
          i += 1;
          while (i < n && isDigit(source[i])) {
            i += 1;
          }
        }
        if (i < n && (source[i] === "e" || source[i] === "E")) {
          i += 1;
          if (i < n && (source[i] === "+" || source[i] === "-")) {
            i += 1;
          }
          while (i < n && isDigit(source[i])) {
            i += 1;
          }
        }
      }
      tokens.push({ kind: "number", value: source.slice(start, i), line, start, end: i });
      continue;
    }

    // 其它单字符标点（`{` `}` `;` `.` 等；多字符运算符拆开即可，扫描器不关心）
    tokens.push({ kind: "punct", value: c, line, start: i, end: i + 1 });
    i += 1;
  }

  return tokens;
}

const DECL_KEYWORDS: Record<string, DeclKind> = {
  struct: "struct",
  event: "event",
  controller: "controller",
  handler: "handler",
  func: "func",
  namespace: "namespace",
  static: "static",
  enum: "enum",
  const: "const",
};

// ---------------------------------------------------------------------------
// 公共扫描接口
// ---------------------------------------------------------------------------

/**
 * 扫描顶层（brace depth 0）声明：struct/event/controller/handler/func/namespace/
 * static/enum/const + 紧随其后的标识符。忽略注释与字符串里的假声明。
 */
export function parseDeclarations(source: string): DeclInfo[] {
  const decls: DeclInfo[] = [];
  if (!source) {
    return decls;
  }
  const tokens = scanWellTokens(source);
  let depth = 0;
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token.kind === "punct") {
      if (token.value === "{") {
        depth += 1;
      } else if (token.value === "}") {
        depth = Math.max(0, depth - 1);
      }
      continue;
    }
    if (depth !== 0 || token.kind !== "word") {
      continue;
    }
    const kind = DECL_KEYWORDS[token.value];
    if (kind === undefined) {
      continue;
    }
    const nameToken = tokens[i + 1];
    if (nameToken === undefined || nameToken.kind !== "word") {
      continue;
    }
    decls.push({ name: nameToken.value, kind, line: token.line });
    i += 1; // 跳过声明名，避免 `struct enum` 这类畸形输入被重复识别
  }
  return decls;
}

/** 扫描顶层 `import "..."` / `import a.b;` 两种写法；start/end 含分号。 */
export function parseImports(source: string): ImportInfo[] {
  const imports: ImportInfo[] = [];
  if (!source) {
    return imports;
  }
  const tokens = scanWellTokens(source);
  let depth = 0;
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token.kind === "punct") {
      if (token.value === "{") {
        depth += 1;
      } else if (token.value === "}") {
        depth = Math.max(0, depth - 1);
      }
      continue;
    }
    if (depth !== 0 || token.kind !== "word" || token.value !== "import") {
      continue;
    }

    let j = i + 1;
    let spec: string | null = null;
    const first = tokens[j];
    if (first !== undefined && first.kind === "string") {
      spec = first.value;
      j += 1;
    } else if (first !== undefined && first.kind === "word") {
      const parts: string[] = [first.value];
      j += 1;
      while (
        tokens[j] !== undefined &&
        tokens[j].kind === "punct" &&
        tokens[j].value === "." &&
        tokens[j + 1] !== undefined &&
        tokens[j + 1].kind === "word"
      ) {
        parts.push(tokens[j + 1].value);
        j += 2;
      }
      spec = parts.join(".");
    }
    if (spec === null) {
      continue;
    }
    const semi = tokens[j];
    if (semi === undefined || semi.kind !== "punct" || semi.value !== ";") {
      continue; // 畸形 import（缺分号）不返回，交给编译器报错
    }
    imports.push({ spec, line: token.line, start: token.start, end: semi.end });
    i = j;
  }
  return imports;
}

/** 扫描顶层 `module a.b;`；返回第一个合法声明，否则 null。 */
export function parseModuleDecl(source: string): ModuleInfo | null {
  if (!source) {
    return null;
  }
  const tokens = scanWellTokens(source);
  let depth = 0;
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token.kind === "punct") {
      if (token.value === "{") {
        depth += 1;
      } else if (token.value === "}") {
        depth = Math.max(0, depth - 1);
      }
      continue;
    }
    if (depth !== 0 || token.kind !== "word" || token.value !== "module") {
      continue;
    }
    let j = i + 1;
    const first = tokens[j];
    if (first === undefined || first.kind !== "word") {
      continue;
    }
    const parts: string[] = [first.value];
    j += 1;
    while (
      tokens[j] !== undefined &&
      tokens[j].kind === "punct" &&
      tokens[j].value === "." &&
      tokens[j + 1] !== undefined &&
      tokens[j + 1].kind === "word"
    ) {
      parts.push(tokens[j + 1].value);
      j += 2;
    }
    const semi = tokens[j];
    if (semi === undefined || semi.kind !== "punct" || semi.value !== ";") {
      continue;
    }
    return { name: parts.join("."), line: token.line, start: token.start, end: semi.end };
  }
  return null;
}

// ---------------------------------------------------------------------------
// import 路径解析（必须与 compiler/wellc/driver.py::_resolve_import 一致）
// ---------------------------------------------------------------------------

function isFile(candidate: string): boolean {
  try {
    return fs.statSync(candidate).isFile();
  } catch {
    return false;
  }
}

/**
 * 把 import spec 解析为绝对文件路径；解析不到返回 null。
 *
 * 对齐 driver.py::_resolve_import(spec, base_dir)：
 * - spec 以 `.` 开头、含 `/` 或 `\`、或以 `.we` 结尾 → 视为文件路径：
 *   `base/spec`，若不以 `.we` 结尾再试 `base/spec.we`；
 * - 否则视为点路径：`a.b` → `base/a/b.we`、`base/a/b/index.we`、
 *   `base/b.we`（同目录 basename 回退），按顺序取第一个存在的文件。
 *
 * @param fromFile 当前文件（绝对或相对路径，取其所在目录为 base_dir）
 */
export function resolveImportSpec(spec: string, fromFile: string): string | null {
  const baseDir = path.dirname(path.resolve(fromFile));
  const candidates: string[] = [];

  if (spec.startsWith(".") || spec.includes("/") || spec.includes("\\") || spec.endsWith(".we")) {
    candidates.push(path.resolve(baseDir, spec));
    if (!spec.endsWith(".we")) {
      candidates.push(path.resolve(baseDir, spec + ".we"));
    }
  } else {
    // 与 Python 的 spec.replace(".", os.sep) 等价：替换所有 `.`
    const rel = spec.split(".").join(path.sep);
    candidates.push(path.resolve(baseDir, rel + ".we"));
    candidates.push(path.resolve(baseDir, rel, "index.we"));
    candidates.push(path.resolve(baseDir, path.basename(rel) + ".we"));
  }

  for (const candidate of candidates) {
    if (isFile(candidate)) {
      return candidate;
    }
  }
  return null;
}

/**
 * 生成从 `fromFile` 指向 `toFile` 的 import spec：
 * 相对路径、正斜杠、保留 `.we` 后缀、无 `./` 前缀；跨盘时返回绝对路径。
 */
export function importSpecFor(fromFile: string, toFile: string): string {
  const from = path.resolve(fromFile);
  let target = path.resolve(toFile);
  if (!/\.we$/i.test(target)) {
    target += ".we";
  }

  const rel = path.relative(path.dirname(from), target);
  if (rel.length === 0) {
    return path.basename(target).split(path.sep).join("/");
  }
  if (path.isAbsolute(rel)) {
    // Windows 跨盘符：path.relative 返回绝对路径，driver 也能接受绝对 spec
    return target.split(path.sep).join("/");
  }
  let spec = rel.split(path.sep).join("/");
  while (spec.startsWith("./")) {
    spec = spec.slice(2);
  }
  return spec;
}
