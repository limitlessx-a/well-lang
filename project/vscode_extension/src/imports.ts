/**
 * Well 的 import 整理与自动插入。
 *
 * 纯 TypeScript（不 import `vscode`），可被 node 直接单测。
 * 设计原则：
 *  - 只重写文件头部“module 行 + 连续 import 块”及其后的空行；import 块之后的正文
 *    逐字节保留（CRLF/LF、BOM、缩进都不动）。
 *  - 解析不了的 import（文件缺失）或拿不到被导入文件声明名（declsOf 返回 null）
 *    一律保留，绝不误删。
 *  - 未使用判定保守：只要正文（跳过注释与字符串）任何位置出现被导入文件的顶层
 *    声明名，就视为使用。
 */

import * as path from "path";
import {
  ImportInfo,
  ModuleInfo,
  parseImports,
  parseModuleDecl,
  resolveImportSpec,
} from "./wellParse";

export interface ImportContext {
  /** 当前文件绝对路径。 */
  file: string;
  /** 被导入文件的顶层声明名；读不到返回 null。 */
  declsOf: (absPath: string) => string[] | null;
}

// ---------------------------------------------------------------------------
// 基础工具
// ---------------------------------------------------------------------------

const IDENTIFIER_RE_SOURCE = "[\\p{L}_][\\p{L}\\p{N}_]*";
const DOTTED_SPEC_RE = /^[\p{L}_][\p{L}\p{N}_]*(?:\.[\p{L}_][\p{L}\p{N}_]*)*$/u;

/** 换行风格：出现 CRLF 就按 CRLF 输出，否则 LF。 */
function detectNewline(source: string): string {
  return source.includes("\r\n") ? "\r\n" : "\n";
}

function endsWithLineBreak(text: string): boolean {
  return text.endsWith("\n") || text.endsWith("\r");
}

/** 路径比较用的键：Windows 大小写不敏感。 */
function pathKey(file: string): string {
  const resolved = path.resolve(file);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

/** 从 offset 处所在行的下一行行首（`; // comment` 会整行跳过）。 */
function lineEndAfter(source: string, offset: number): number {
  const newline = source.indexOf("\n", offset);
  return newline < 0 ? source.length : newline + 1;
}

/** 把路径类 spec 统一成正斜杠。 */
function withForwardSlashes(spec: string): string {
  return spec.split("\\").join("/");
}

/** 把注释与字符串替换为空格（保留换行），用于“正文标识符”扫描。 */
function stripCommentsAndStrings(source: string): string {
  const out: string[] = [];
  const n = source.length;
  let i = 0;
  while (i < n) {
    const c = source[i];
    if (c === "/" && source[i + 1] === "/") {
      while (i < n && source[i] !== "\n") {
        out.push(" ");
        i += 1;
      }
      continue;
    }
    if (c === "/" && source[i + 1] === "*") {
      out.push(" ", " ");
      i += 2;
      while (i < n && !(source[i] === "*" && source[i + 1] === "/")) {
        out.push(source[i] === "\n" ? "\n" : " ");
        i += 1;
      }
      if (i < n) {
        out.push(" ", " ");
        i += 2;
      }
      continue;
    }
    if (c === '"') {
      out.push(" ");
      i += 1;
      while (i < n && source[i] !== '"') {
        if (source[i] === "\\" && i + 1 < n) {
          out.push(" ", " ");
          i += 2;
          continue;
        }
        out.push(source[i] === "\n" ? "\n" : " ");
        i += 1;
      }
      if (i < n) {
        out.push(" ");
        i += 1;
      }
      continue;
    }
    out.push(c);
    i += 1;
  }
  return out.join("");
}

/** 收集文本里的标识符集合（应传入已去掉注释/字符串的文本）。 */
function collectIdentifiers(code: string): Set<string> {
  const identifiers = new Set<string>();
  const re = new RegExp(IDENTIFIER_RE_SOURCE, "gu");
  let match = re.exec(code);
  while (match !== null) {
    identifiers.add(match[0]);
    match = re.exec(code);
  }
  return identifiers;
}

// ---------------------------------------------------------------------------
// 内部布局分析
// ---------------------------------------------------------------------------

interface ImportLine extends ImportInfo {
  /** 源码里是否写成 `import "..."`（否则是点路径 `import a.b;`）。 */
  quoted: boolean;
  /** 解析到的绝对路径；解析失败为 null。 */
  resolved: string | null;
}

interface Layout {
  /** 原始源码（含 BOM）。 */
  source: string;
  newline: string;
  /** 位于 import 块之前的顶层 module 声明（用于整理）。 */
  module: ModuleInfo | null;
  /** 所有顶层 import（按出现位置）。 */
  imports: ImportLine[];
  /** 文件头部、module 之后连续的 import 块（整理只动这一块）。 */
  block: ImportLine[];
  /** module 行与第一条 import 之间的原始文本（只去掉行首空行，保留注释/空行）。 */
  leadGap: string | null;
  /** import 组内部各 import 之间的注释（重排时不能丢，统一放到导入组之前输出）。 */
  comments: string[];
}

/** 从“只可能含空白/注释”的文本里提取注释原文（保留块注释内部换行）。 */
function extractComments(text: string): string[] {
  const comments: string[] = [];
  const n = text.length;
  let i = 0;
  while (i < n) {
    const c = text[i];
    if (c === "/" && text[i + 1] === "/") {
      const start = i;
      i += 2;
      while (i < n && text[i] !== "\n") {
        i += 1;
      }
      comments.push(text.slice(start, i).replace(/\s+$/, ""));
      continue;
    }
    if (c === "/" && text[i + 1] === "*") {
      const start = i;
      i += 2;
      while (i < n && !(text[i] === "*" && text[i + 1] === "/")) {
        i += 1;
      }
      if (i < n) {
        i += 2;
      }
      comments.push(text.slice(start, i));
      continue;
    }
    if (c === '"') {
      i += 1;
      while (i < n && text[i] !== '"') {
        i += text[i] === "\\" ? 2 : 1;
      }
      if (i < n) {
        i += 1;
      }
      continue;
    }
    i += 1;
  }
  return comments;
}

function analyze(source: string, file: string): Layout {
  const newline = detectNewline(source);
  const module = parseModuleDecl(source);
  const imports: ImportLine[] = parseImports(source).map((info) => {
    const text = source.slice(info.start, info.end);
    let resolved: string | null = null;
    try {
      resolved = resolveImportSpec(info.spec, file);
    } catch {
      resolved = null;
    }
    return { ...info, quoted: text.includes('"'), resolved };
  });

  const headerModule =
    module !== null && (imports.length === 0 || module.start < imports[0].start) ? module : null;

  // 头部连续 import 块：module（或无 module 时文件开头）与 import 之间只允许
  // 空白和注释；一旦出现代码（例如已经进入声明区），后面的 import 不再整理。
  let prevEnd = headerModule !== null ? headerModule.end : 0;
  const block: ImportLine[] = [];
  const comments: string[] = [];
  for (const importLine of imports) {
    if (importLine.start < prevEnd) {
      continue;
    }
    const gap = source.slice(prevEnd, importLine.start);
    const gapCode = stripCommentsAndStrings(gap).trim();
    if (gapCode.length > 0) {
      break;
    }
    // 后续每条 import 之间的注释也要收集，避免重排时丢注释。
    // （第一条 import 之前的这段文本由 prefix / leadGap 原样保留。）
    if (block.length > 0) {
      for (const comment of extractComments(gap)) {
        comments.push(comment);
      }
    }
    block.push(importLine);
    prevEnd = importLine.end;
  }

  const leadGap =
    headerModule !== null && block.length > 0 ? source.slice(headerModule.end, block[0].start) : null;

  return { source, newline, module: headerModule, imports, block, leadGap, comments };
}

// ---------------------------------------------------------------------------
// 未使用判定
// ---------------------------------------------------------------------------

function importIsUsed(
  importLine: ImportLine,
  ctx: ImportContext,
  bodyIdentifiers: Set<string>
): boolean {
  if (importLine.resolved === null) {
    return true; // 解析不到 → 保留，绝不误删
  }
  let decls: string[] | null;
  try {
    decls = ctx.declsOf(importLine.resolved);
  } catch {
    decls = null;
  }
  if (decls === null) {
    return true; // 文件读不到/未索引 → 保留
  }
  for (const name of decls) {
    if (bodyIdentifiers.has(name)) {
      return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

function renderImportLine(importLine: ImportLine): string {
  if (importLine.quoted) {
    return `import "${withForwardSlashes(importLine.spec)}";`;
  }
  return `import ${importLine.spec};`;
}

function compareImports(a: ImportLine, b: ImportLine): number {
  const la = a.spec.toLowerCase();
  const lb = b.spec.toLowerCase();
  if (la !== lb) {
    return la < lb ? -1 : 1;
  }
  if (a.spec !== b.spec) {
    return a.spec < b.spec ? -1 : 1;
  }
  return a.start - b.start;
}

/** 头部重建：module 行 + import 组 + 与正文之间恰好一个空行。 */
function renderHeader(
  layout: Layout,
  imports: ImportLine[],
  tailAfterLastOriginalImport: string
): string {
  const newline = layout.newline;
  const source = layout.source;
  const firstBlockImport = layout.block[0];
  const prefixEnd =
    layout.module !== null ? layout.module.start : firstBlockImport !== undefined ? firstBlockImport.start : 0;

  let out = source.slice(0, prefixEnd);
  const moduleText =
    layout.module !== null ? source.slice(layout.module.start, layout.module.end) : null;
  // import 块之后正文：去掉紧跟在最后一条 import 之后的空行，正文本身逐字节保留
  const rest = tailAfterLastOriginalImport.replace(/^(?:[ \t]*\r?\n)*/, "");

  if (moduleText !== null) {
    out += moduleText + newline;
  }

  // module 与第一条 import 之间原样保留（只去掉行首空行；注释与空行都不丢）
  const leadGap =
    layout.leadGap === null ? "" : layout.leadGap.replace(/^(?:[ \t]*\r?\n)*/, "");

  // import 组 = 中间注释（保留）+ 排序后的 import 行；每条一行。
  const group: string[] = [];
  for (const comment of layout.comments) {
    group.push(comment + newline);
  }
  for (const importLine of imports) {
    group.push(renderImportLine(importLine) + newline);
  }

  if (leadGap.length > 0) {
    out += newline + leadGap; // module 行后空一行，再接原有注释/空行
  } else if (group.length > 0 && moduleText !== null) {
    out += newline; // module 行与导入组之间空一行
  }
  if (group.length > 0) {
    out += group.join("");
  }

  if (rest.length > 0) {
    if (group.length === 0 && leadGap.length > 0) {
      // 所有 import 都被移除时，避免注释后留下连续空行
      out = out.replace(/(?:\r?\n)+$/, newline);
    }
    if (out.length > 0) {
      if (!endsWithLineBreak(out)) {
        // 仅剩 BOM 时直接接正文（BOM 不计作一行）
        if (out !== "\uFEFF") {
          out += newline;
        }
      } else {
        out += newline; // 导入组（或 module 行）与第一个声明之间空一行
      }
    }
    out += rest;
  }

  return out;
}

// ---------------------------------------------------------------------------
// 公共接口
// ---------------------------------------------------------------------------

/**
 * 整理 import：按 spec（忽略大小写）排序 + 去重 + 移除未使用的 import。
 * 无变化时返回原字符串（严格相等）；只重写头部，正文逐字节保留。
 */
export function organizeImports(source: string, ctx: ImportContext): string {
  const layout = analyze(source, ctx.file);
  if (layout.block.length === 0) {
    return source;
  }
  const lastImport = layout.block[layout.block.length - 1];
  const body = source.slice(lastImport.end);
  const bodyIdentifiers = collectIdentifiers(stripCommentsAndStrings(body));

  const used: ImportLine[] = [];
  for (const importLine of layout.block) {
    if (importIsUsed(importLine, ctx, bodyIdentifiers)) {
      used.push(importLine);
    }
  }

  const seen = new Set<string>();
  const unique: ImportLine[] = [];
  for (const importLine of used) {
    const key =
      importLine.resolved !== null
        ? `path:${pathKey(importLine.resolved)}`
        : `spec:${importLine.spec.toLowerCase()}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    unique.push(importLine);
  }
  unique.sort(compareImports);

  const organized = renderHeader(layout, unique, body);
  return organized === source ? source : organized;
}

/**
 * 加入一条 import：已存在（文本等价或解析到同一文件）时原样返回；
 * 否则插入到 import 组末尾（无 import 时插到 module 行之后；无 module 时文件顶部）。
 */
export function addImport(source: string, ctx: ImportContext, spec: string): string {
  if (spec.trim().length === 0) {
    return source;
  }
  const layout = analyze(source, ctx.file);
  if (importSpecExists(source, spec)) {
    return source;
  }
  const target = resolveImportSpec(spec, ctx.file);
  if (target !== null) {
    for (const importLine of layout.imports) {
      if (importLine.resolved !== null && pathKey(importLine.resolved) === pathKey(target)) {
        return source;
      }
    }
  }

  const newline = layout.newline;
  const normalized = withForwardSlashes(spec.trim());
  const newLine = DOTTED_SPEC_RE.test(normalized)
    ? `import ${normalized};`
    : `import "${normalized}";`;
  const raw = layout.source;

  // 1) 已有 import 块：插到最后一条 import 的下一行
  if (layout.block.length > 0) {
    const lastImport = layout.block[layout.block.length - 1];
    const insertAt = lineEndAfter(raw, lastImport.end);
    const head = raw.slice(0, insertAt);
    const tail = raw.slice(insertAt).replace(/^(?:[ \t]*\r?\n)*/, "");
    let out = head;
    if (!endsWithLineBreak(out)) {
      out += newline;
    }
    out += newLine + newline;
    if (tail.length > 0) {
      out += newline + tail;
    }
    return out;
  }

  // 2) 有 module、无 import：插到 module 行之后，组后空一行
  if (layout.module !== null) {
    const insertAt = lineEndAfter(raw, layout.module.end);
    const head = raw.slice(0, insertAt);
    const tail = raw.slice(insertAt).replace(/^(?:[ \t]*\r?\n)*/, "");
    let out = head;
    if (!endsWithLineBreak(out)) {
      out += newline;
    }
    out += newline + newLine + newline;
    if (tail.length > 0) {
      out += newline + tail;
    }
    return out;
  }

  // 3) 无 module、无 import：插到文件顶部（BOM 之后），组后空一行
  const bomLength = raw.charCodeAt(0) === 0xfeff ? 1 : 0;
  const head = raw.slice(0, bomLength);
  const tail = raw.slice(bomLength).replace(/^(?:[ \t]*\r?\n)*/, "");
  let out = head + newLine + newline;
  if (tail.length > 0) {
    out += newline + tail;
  }
  return out;
}

/** 找出所有未使用的 import（仅当能解析且拿得到声明名时才判定）。 */
export function findUnusedImports(source: string, ctx: ImportContext): ImportInfo[] {
  const layout = analyze(source, ctx.file);
  if (layout.block.length === 0) {
    return [];
  }
  const lastImport = layout.block[layout.block.length - 1];
  const bodyIdentifiers = collectIdentifiers(stripCommentsAndStrings(source.slice(lastImport.end)));
  const unused: ImportInfo[] = [];
  for (const importLine of layout.block) {
    if (!importIsUsed(importLine, ctx, bodyIdentifiers)) {
      unused.push({
        spec: importLine.spec,
        line: importLine.line,
        start: importLine.start,
        end: importLine.end,
      });
    }
  }
  return unused;
}

/**
 * 文本上是否已经存在等价 import（忽略 `./` 前缀、反斜杠、`.we` 后缀差异）。
 * 注意：本函数没有 fromFile，不能做“解析后同一文件”的判断（那是 addImport 的职责）。
 */
export function importSpecExists(source: string, spec: string): boolean {
  const wanted = normalizeSpecForCompare(spec);
  if (wanted.length === 0) {
    return false;
  }
  for (const importInfo of parseImports(source)) {
    if (normalizeSpecForCompare(importInfo.spec) === wanted) {
      return true;
    }
  }
  return false;
}

function normalizeSpecForCompare(spec: string): string {
  let normalized = withForwardSlashes(spec.trim());
  if (normalized.length === 0) {
    return "";
  }
  while (normalized.startsWith("./")) {
    normalized = normalized.slice(2);
  }
  if (normalized.length > 3 && normalized.toLowerCase().endsWith(".we")) {
    normalized = normalized.slice(0, -3);
  }
  return normalized;
}
