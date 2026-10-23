/**
 * Well (.we) 代码格式化器 —— 纯 TypeScript，不依赖 vscode 模块，可直接单测。
 *
 * 设计要点：
 * 1. 先 `tokenizeWell` 得到与 compiler/wellc/lexer.py 一致的 token 流，再按 token 流重排空白；
 *    因此 `tokenizeWell(formatWell(x))` 与 `tokenizeWell(x)` 的 token 序列（kind/value/raw，
 *    含注释）完全一致，只允许空白变化。
 * 2. 字符串、注释原文逐字保留（行注释仅去掉行尾空白）；换行统一为 LF；文件末恰好一个 `\n`。
 * 3. 所有排版决策都只依赖 token 序列本身（不依赖 token 原始换行，除非用于保留最多 1 个空行），
 *    因此输出是确定性的、幂等的：formatWell(formatWell(x)) === formatWell(x)。
 * 4. 对不配对/非法输入不抛异常，仍返回确定性文本。
 *
 * 已知取舍（详见随附报告）：
 * - `{` 是“代码块”还是“容器/事件初始化字面量”用上下文启发式区分：`=`/`(`/`[`/`,`/`:`/`?`/
 *   二元运算符/`return` 之后、`new T(...)` 的 `)` 之后视为字面量，其余（含 `struct/event/
 *   controller/handler/func/namespace/static/enum/switch/if/for/while/else` 之后）视为代码块。
 *   字面量保持单行并写成 `{ a: 1 }` / `{}`。
 * - `Array<int>` 这类泛型尖括号按“前一个 token 以大写字母开头 + 前向扫描只含类型 token”判定为
 *   类型参数（不空格）；否则 `<`/`>` 按二元运算符（两侧空格）处理。
 * - `case`/`default`（词法上不是关键字）在 switch 块内按标签处理：回退一级，冒号后换行，body +1。
 * - 行注释紧跟代码时保留在行尾（`;`/`{`/`case x:` 后延迟换行）；块注释独占行、原文保留。
 */

export interface FormatOptions {
  tabSize?: number;
  insertSpaces?: boolean;
}

export interface WellToken {
  kind: "ident" | "keyword" | "number" | "string" | "comment" | "op" | "eof";
  value: string;
  raw: string;
  line: number;
  col: number;
}

/** 与 compiler/wellc/lexer.py 的 KEYWORDS 保持一致。 */
const KEYWORDS: ReadonlySet<string> = new Set<string>([
  "module", "import", "entry", "const", "enum", "struct", "event",
  "controller", "handler", "func", "namespace", "static", "extends",
  "var", "new", "if", "else", "while", "switch",
  "for", "return", "break", "continue", "true", "false", "null",
  "void", "bool", "int", "long", "float", "double", "String",
]);

/** 与 lexer.py 的 MULTI_OPS 保持一致（按 lexer 中的顺序匹配即可，互不为前缀冲突）。 */
const MULTI_OPS: readonly string[] = ["==", "!=", "<=", ">=", "&&", "||", "??", "+=", "-=", "->"];

// 注意：lexer.py 的 SINGLE_OPS（{}()[];,:.?+-*/%<>=!&|^~）中的字符，以及未知字符，
// 在下面的 tokenizeWell 中都统一按 kind="op" 处理（容错，不抛异常）。

/** 可以出现在类型参数里的关键字。 */
const TYPE_KEYWORDS: ReadonlySet<string> = new Set<string>(["void", "bool", "int", "long", "float", "double", "String"]);

/** 后接 ` (` 的控制关键字。 */
const CONTROL_PAREN_KEYWORDS: ReadonlySet<string> = new Set<string>(["if", "while", "for", "switch"]);

/** 两侧加空格的二元/赋值运算符。 */
const BINARY_OPS: ReadonlySet<string> = new Set<string>([
  "=", "+=", "-=", "==", "!=", "<=", ">=", "&&", "||", "??",
  "+", "-", "*", "/", "%", "<", ">", "&", "|", "^",
]);

/** 若 `-`/`+` 的前一个 token 在下表中，则它是（前缀）一元运算符。 */
const UNARY_PREV_OPS: ReadonlySet<string> = new Set<string>([
  "(", "[", "{", ",", ";", ":", "?", "=", "+=", "-=",
  "&&", "||", "??", "==", "!=", "<=", ">=", "<", ">",
  "+", "-", "*", "/", "%", "!", "~", "&", "|", "^",
]);

function normalizeNewlines(source: string): string {
  if (source.indexOf("\r") === -1) {
    return source;
  }
  return source.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

function isDigit(ch: string): boolean {
  return ch >= "0" && ch <= "9";
}

function isHexDigit(ch: string): boolean {
  return (ch >= "0" && ch <= "9") || (ch >= "a" && ch <= "f") || (ch >= "A" && ch <= "F");
}

function isIdentStart(ch: string): boolean {
  return (ch >= "a" && ch <= "z") || (ch >= "A" && ch <= "Z") || ch === "_";
}

function isIdentPart(ch: string): boolean {
  return isIdentStart(ch) || isDigit(ch);
}

/**
 * Well 词法分析：行为对齐 compiler/wellc/lexer.py，但更宽容：
 * 未闭合的字符串/块注释、未知字符都不会抛异常，而是尽量产出 token（格式化器要求“不崩溃”）。
 * 注释作为 kind="comment" 的 token 保留（行注释 raw 去掉行尾空白）。
 */
export function tokenizeWell(source: string): WellToken[] {
  const src = normalizeNewlines(source);
  const toks: WellToken[] = [];
  const n = src.length;
  let i = 0;
  let line = 1;
  let col = 1;

  while (i < n) {
    const ch = src[i];

    if (ch === " " || ch === "\t") {
      i += 1;
      col += 1;
      continue;
    }
    if (ch === "\n") {
      i += 1;
      line += 1;
      col = 1;
      continue;
    }

    const tokLine = line;
    const tokCol = col;

    if (src.startsWith("//", i)) {
      const start = i;
      while (i < n && src[i] !== "\n") {
        i += 1;
      }
      const raw = src.slice(start, i).replace(/[ \t]+$/, "");
      toks.push({ kind: "comment", value: raw, raw, line: tokLine, col: tokCol });
      col += i - start;
      continue;
    }

    if (src.startsWith("/*", i)) {
      const start = i;
      i += 2;
      col += 2;
      while (i < n) {
        if (src.startsWith("*/", i)) {
          i += 2;
          col += 2;
          break;
        }
        if (src[i] === "\n") {
          line += 1;
          col = 1;
        } else {
          col += 1;
        }
        i += 1;
      }
      const raw = src.slice(start, i);
      toks.push({ kind: "comment", value: raw, raw, line: tokLine, col: tokCol });
      continue;
    }

    if (isIdentStart(ch)) {
      const start = i;
      while (i < n && isIdentPart(src[i])) {
        i += 1;
      }
      const raw = src.slice(start, i);
      col += raw.length;
      toks.push({
        kind: KEYWORDS.has(raw) ? "keyword" : "ident",
        value: raw,
        raw,
        line: tokLine,
        col: tokCol,
      });
      continue;
    }

    if (isDigit(ch) || (ch === "." && i + 1 < n && isDigit(src[i + 1]))) {
      const start = i;
      if (src.startsWith("0x", i) || src.startsWith("0X", i)) {
        i += 2;
        while (i < n && isHexDigit(src[i])) {
          i += 1;
        }
      } else {
        while (i < n && isDigit(src[i])) {
          i += 1;
        }
        if (i < n && src[i] === ".") {
          i += 1;
          while (i < n && isDigit(src[i])) {
            i += 1;
          }
        }
        if (i < n && (src[i] === "e" || src[i] === "E")) {
          i += 1;
          if (i < n && (src[i] === "+" || src[i] === "-")) {
            i += 1;
          }
          while (i < n && isDigit(src[i])) {
            i += 1;
          }
        }
      }
      const raw = src.slice(start, i);
      col += raw.length;
      toks.push({ kind: "number", value: raw, raw, line: tokLine, col: tokCol });
      continue;
    }

    if (ch === '"') {
      const start = i;
      i += 1;
      col += 1;
      const buf: string[] = [];
      const escapes: Record<string, string> = { n: "\n", t: "\t", r: "\r", "\\": "\\", '"': '"', "0": "\0" };
      while (i < n) {
        const c = src[i];
        if (c === '"') {
          i += 1;
          col += 1;
          break;
        }
        if (c === "\\") {
          i += 1;
          col += 1;
          if (i >= n) {
            break;
          }
          const esc = src[i];
          buf.push(Object.prototype.hasOwnProperty.call(escapes, esc) ? escapes[esc] : esc);
          i += 1;
          col += 1;
          continue;
        }
        if (c === "\n") {
          line += 1;
          col = 1;
        } else {
          col += 1;
        }
        buf.push(c);
        i += 1;
      }
      const raw = src.slice(start, i);
      toks.push({ kind: "string", value: buf.join(""), raw, line: tokLine, col: tokCol });
      continue;
    }

    let multi: string | null = null;
    for (const op of MULTI_OPS) {
      if (src.startsWith(op, i)) {
        multi = op;
        break;
      }
    }
    if (multi !== null) {
      toks.push({ kind: "op", value: multi, raw: multi, line: tokLine, col: tokCol });
      i += multi.length;
      col += multi.length;
      continue;
    }

    // 已知的单字符运算符；未知字符也按 op 吞掉（容错，不抛异常）。
    toks.push({ kind: "op", value: ch, raw: ch, line: tokLine, col: tokCol });
    i += 1;
    col += 1;
    continue;
  }

  toks.push({ kind: "eof", value: "", raw: "", line, col });
  return toks;
}

type BraceKind = "block" | "literal";

interface Frame {
  kind: "root" | "paren" | "bracket" | "block" | "literal";
  ternary: number;
  isSwitch: boolean;
  inCaseBody: boolean;
}

/** 判断 `<` 是否为泛型参数开始（`Array<int>` / `Map<String>` / `Linked<ItemVo>`）。 */
function isGenericOpen(tokens: WellToken[], index: number, prev: WellToken | null): boolean {
  if (prev === null) {
    return false;
  }
  if (prev.kind !== "ident" && prev.kind !== "keyword") {
    return false;
  }
  // 类型名约定以大写字母开头（Array / Map / Linked / 自定义 Vo 类型 …）。
  if (!/^[A-Z]/.test(prev.value)) {
    return false;
  }
  let depth = 0;
  for (let j = index; j < tokens.length; j += 1) {
    const t = tokens[j];
    if (t.kind === "eof") {
      return false;
    }
    if (t.kind === "comment") {
      continue;
    }
    const v = t.value;
    if (v === "<") {
      depth += 1;
      continue;
    }
    if (v === ">") {
      depth -= 1;
      if (depth === 0) {
        return true;
      }
      continue;
    }
    if (depth === 0) {
      return false;
    }
    if (t.kind === "ident") {
      continue;
    }
    if (t.kind === "keyword" && TYPE_KEYWORDS.has(v)) {
      continue;
    }
    if (v === "." || v === ",") {
      continue;
    }
    return false;
  }
  return false;
}

/**
 * 按 Well 词法规则重新排版源码。
 * @param source 源码（可含 CRLF）
 * @param options 缩进选项：tabSize 默认 4，insertSpaces 默认 true
 */
export function formatWell(source: string, options?: FormatOptions): string {
  const tokens = tokenizeWell(source);

  const rawTabSize = options === undefined ? undefined : options.tabSize;
  let tabSize = 4;
  if (typeof rawTabSize === "number" && Number.isFinite(rawTabSize) && rawTabSize >= 0) {
    tabSize = Math.min(16, Math.floor(rawTabSize));
  }
  const insertSpaces = options === undefined ? true : options.insertSpaces !== false;
  const indentUnit = insertSpaces ? " ".repeat(tabSize) : "\t";

  const frames: Frame[] = [{ kind: "root", ternary: 0, isSwitch: false, inCaseBody: false }];
  const parenNewFlags: boolean[] = [];

  const outLines: string[] = [];
  let cur = "";
  let atLineStart = true;
  let pendingSpace = false;
  let blankAllowed = true;

  let lastToken: WellToken | null = null;
  let lastTokenLine: number | null = null;
  let prevCode: WellToken | null = null;

  let parenDepth = 0;
  let newCallPending = false;
  let lastClosedParenIsNew = false;
  let pendingSwitch = false;
  let justClosedBlock = false;
  let caseLabelFrame: Frame | null = null;
  let caseLabelActive = false;
  let angleDepth = 0;
  let lastGenericClose = false;
  let afterCaseColon = false;

  function currentIndent(): number {
    let level = 0;
    for (const frame of frames) {
      if (frame.kind === "block") {
        level += 1;
        if (frame.isSwitch && frame.inCaseBody) {
          level += 1;
        }
      }
    }
    return level;
  }

  function indentString(): string {
    const level = currentIndent();
    return level === 0 ? "" : indentUnit.repeat(level);
  }

  function write(text: string): void {
    if (atLineStart) {
      cur = indentString();
      atLineStart = false;
    } else if (pendingSpace && cur.length > 0 && cur[cur.length - 1] !== " ") {
      cur += " ";
    }
    cur += text;
    pendingSpace = false;
    blankAllowed = true;
  }

  /** 写入可能包含换行符的原文（多行字符串/块注释），后续行逐字保留。 */
  function writeRawMultiline(text: string): void {
    const parts = text.split("\n");
    if (parts.length === 1) {
      write(parts[0]);
      return;
    }
    write(parts[0]);
    forceNewline();
    for (let k = 1; k < parts.length - 1; k += 1) {
      outLines.push(parts[k]);
    }
    cur = parts[parts.length - 1];
    atLineStart = false;
    pendingSpace = false;
  }

  function forceNewline(): void {
    if (!atLineStart) {
      outLines.push(cur.replace(/[ \t]+$/, ""));
      cur = "";
      atLineStart = true;
    }
    pendingSpace = false;
  }

  /** 行尾若有原始行注释（同一行），延迟换行，让注释留在代码行尾。 */
  function endStatementLine(token: WellToken, next: WellToken | undefined): void {
    if (
      next !== undefined &&
      next.kind === "comment" &&
      next.raw.startsWith("//") &&
      next.line === token.line
    ) {
      pendingSpace = true;
      return;
    }
    forceNewline();
  }

  function beginToken(token: WellToken, allow?: boolean): void {
    if (!atLineStart) {
      return;
    }
    const canBlank = allow === undefined ? blankAllowed : allow;
    if (
      canBlank &&
      lastTokenLine !== null &&
      outLines.length > 0 &&
      token.line - lastTokenLine >= 2 &&
      outLines[outLines.length - 1] !== ""
    ) {
      outLines.push("");
    }
  }

  function topFrame(): Frame {
    return frames[frames.length - 1];
  }

  function mark(token: WellToken, contentLine?: number): void {
    lastToken = token;
    lastTokenLine = contentLine === undefined ? token.line : contentLine;
  }

  function classifyBrace(prevWasCaseColon: boolean, prevWasGenericClose: boolean): BraceKind {
    const prev = prevCode;
    if (prev === null) {
      return "block";
    }
    if (prevWasCaseColon) {
      return "block";
    }
    const v = prev.value;
    if (prev.kind === "op") {
      if (
        v === "=" || v === "(" || v === "[" || v === "," ||
        v === ":" || v === "?" || v === "+=" || v === "-="
      ) {
        return "literal";
      }
      if (v === ")") {
        return lastClosedParenIsNew ? "literal" : "block";
      }
      // 泛型返回类型：`func f(): Array<int> {` 的 `{` 是代码块。
      if (v === ">" && prevWasGenericClose) {
        return "block";
      }
      if (BINARY_OPS.has(v)) {
        return "literal";
      }
      return "block";
    }
    if (prev.kind === "keyword" && v === "return") {
      return "literal";
    }
    return "block";
  }

  function isUnaryPosition(prev: WellToken | null): boolean {
    if (prev === null) {
      return true;
    }
    if (prev.kind === "op") {
      return UNARY_PREV_OPS.has(prev.value);
    }
    if (prev.kind === "keyword") {
      return prev.value === "return";
    }
    // `case`/`default` 在词法上不是关键字。
    return prev.kind === "ident" && (prev.value === "case" || prev.value === "default");
  }

  function isBlockCloseContinuation(token: WellToken): boolean {
    if (token.kind === "eof") {
      return true;
    }
    if (token.kind === "comment") {
      return lastToken !== null && token.line === lastToken.line;
    }
    return (
      token.value === "else" || token.value === ";" || token.value === ")" ||
      token.value === "," || token.value === "." || token.value === "->" || token.value === "]" ||
      token.value === "}"
    );
  }

  function handleComment(token: WellToken): void {
    const newlines = token.raw.split("\n").length - 1;
    const isLineComment = token.raw.startsWith("//");
    if (isLineComment) {
      const trailing = !atLineStart && lastToken !== null && token.line === lastToken.line;
      if (trailing) {
        pendingSpace = true;
        write(token.raw);
      } else {
        forceNewline();
        beginToken(token);
        write(token.raw);
      }
      forceNewline();
    } else {
      forceNewline();
      beginToken(token);
      writeRawMultiline(token.raw);
      forceNewline();
    }
    mark(token, token.line + newlines);
  }

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token.kind === "eof") {
      break;
    }
    const value = token.value;
    const nextToken: WellToken | undefined = tokens[i + 1];
    const prevWasGenericClose = lastGenericClose;
    lastGenericClose = false;
    const prevWasCaseColon = afterCaseColon;
    afterCaseColon = false;

    if (justClosedBlock) {
      justClosedBlock = false;
      if (!isBlockCloseContinuation(token)) {
        forceNewline();
      } else if (value === "else") {
        pendingSpace = true;
      }
    }

    if (token.kind === "comment") {
      handleComment(token);
      continue;
    }

    if (token.kind === "op" && value === "{") {
      const isEmpty = nextToken !== undefined && nextToken.kind === "op" && nextToken.value === "}";
      const kind = classifyBrace(prevWasCaseColon, prevWasGenericClose);
      const isNewInit = lastClosedParenIsNew;
      lastClosedParenIsNew = false;
      newCallPending = false;

      if (isEmpty) {
        // 空块 `{}` 与空字面量 `{}` 都保持一行。
        const closeToken = nextToken as WellToken;
        if (kind === "block") {
          if (!atLineStart) {
            pendingSpace = true;
          }
          beginToken(token);
          write("{}");
          pendingSpace = false;
          justClosedBlock = true;
        } else {
          if (isNewInit) {
            pendingSpace = true;
          }
          beginToken(token);
          write("{}");
          pendingSpace = true;
        }
        mark(token);
        mark(closeToken);
        prevCode = closeToken;
        i += 1;
        continue;
      }

      if (kind === "literal") {
        if (isNewInit) {
          pendingSpace = true;
        }
        beginToken(token);
        write("{");
        frames.push({ kind: "literal", ternary: 0, isSwitch: false, inCaseBody: false });
        // 字面量内部左右各留 1 空格：`{ "k": 1 }`。
        pendingSpace = true;
      } else {
        if (!atLineStart) {
          pendingSpace = true;
        }
        beginToken(token);
        write("{");
        frames.push({ kind: "block", ternary: 0, isSwitch: pendingSwitch, inCaseBody: false });
        pendingSwitch = false;
        blankAllowed = false;
        endStatementLine(token, nextToken);
      }
      mark(token);
      prevCode = token;
      continue;
    }

    if (token.kind === "op" && value === "}") {
      const top = topFrame();
      if (top.kind === "literal") {
        frames.pop();
        if (!atLineStart) {
          pendingSpace = true;
        }
        write("}");
        pendingSpace = true;
        mark(token);
        prevCode = token;
        continue;
      }
      if (top.kind === "block") {
        if (top.isSwitch) {
          top.inCaseBody = false;
        }
        frames.pop();
      }
      if (!atLineStart) {
        forceNewline();
      }
      beginToken(token, false);
      write("}");
      pendingSpace = false;
      justClosedBlock = true;
      mark(token);
      prevCode = token;
      continue;
    }

    if (token.kind === "op") {
      if (value === "(") {
        if (prevCode !== null &&
            (prevCode.kind === "ident" || prevCode.kind === "keyword" ||
             prevCode.kind === "number" || prevCode.kind === "string")) {
          // 调用/声明形参：`f(`、`func f(`；控制关键字后保留 1 空格：`if (`。
          pendingSpace = prevCode.kind === "keyword" && CONTROL_PAREN_KEYWORDS.has(prevCode.value);
        }
        // 其余情况（`= (`、`+ (`、`, (`、`! (` 等）沿用前一个 token 留下的空格需求。
        beginToken(token);
        write("(");
        frames.push({ kind: "paren", ternary: 0, isSwitch: false, inCaseBody: false });
        parenNewFlags.push(newCallPending);
        newCallPending = false;
        parenDepth += 1;
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === ")") {
        pendingSpace = false;
        write(")");
        if (parenDepth > 0) {
          parenDepth -= 1;
        }
        if (topFrame().kind === "paren") {
          frames.pop();
        }
        lastClosedParenIsNew = parenNewFlags.length > 0 ? parenNewFlags.pop() === true : false;
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === "[") {
        const indexing =
          prevCode !== null &&
          (prevCode.kind === "ident" || prevCode.kind === "number" || prevCode.kind === "string" ||
            (prevCode.kind === "keyword" && prevCode.value !== "return") ||
            prevCode.value === ")" || prevCode.value === "]");
        if (indexing) {
          pendingSpace = false;
        }
        beginToken(token);
        write("[");
        frames.push({ kind: "bracket", ternary: 0, isSwitch: false, inCaseBody: false });
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === "]") {
        pendingSpace = false;
        write("]");
        if (topFrame().kind === "bracket") {
          frames.pop();
        }
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === ",") {
        pendingSpace = false;
        write(",");
        pendingSpace = true;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === "." || value === "->") {
        pendingSpace = false;
        write(token.value);
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === ";") {
        pendingSpace = false;
        write(";");
        if (parenDepth > 0) {
          pendingSpace = true;
        } else {
          endStatementLine(token, nextToken);
        }
        newCallPending = false;
        caseLabelActive = false;
        pendingSwitch = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === ":") {
        if (caseLabelActive) {
          pendingSpace = false;
          write(":");
          if (caseLabelFrame !== null) {
            caseLabelFrame.inCaseBody = true;
          }
          caseLabelFrame = null;
          caseLabelActive = false;
          blankAllowed = false;
          afterCaseColon = true;
          endStatementLine(token, nextToken);
          mark(token);
          prevCode = token;
          continue;
        }
        const frame = topFrame();
        if (frame.ternary > 0) {
          frame.ternary -= 1;
          pendingSpace = true;
          write(":");
          pendingSpace = true;
        } else {
          // 字段/参数/映射键：冒号前不留空格，后留 1 空格。
          pendingSpace = false;
          write(":");
          pendingSpace = true;
        }
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === "?") {
        pendingSpace = true;
        write("?");
        topFrame().ternary += 1;
        pendingSpace = true;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === "!") {
        write("!");
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if ((value === "-" || value === "+" || value === "~") && isUnaryPosition(prevCode)) {
        write(value);
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === "<" && isGenericOpen(tokens, i, prevCode)) {
        angleDepth += 1;
        pendingSpace = false;
        write("<");
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      if (value === ">" && angleDepth > 0) {
        angleDepth -= 1;
        lastGenericClose = true;
        pendingSpace = false;
        write(">");
        pendingSpace = false;
        mark(token);
        prevCode = token;
        continue;
      }
      // 其余二元运算 / 赋值：两侧各 1 空格。
      pendingSpace = true;
      write(value);
      pendingSpace = true;
      mark(token);
      prevCode = token;
      continue;
    }

    // ident / keyword / number / string
    const isCaseLabel =
      (value === "case" || value === "default") &&
      topFrame().kind === "block" &&
      topFrame().isSwitch;
    if (isCaseLabel) {
      if (!atLineStart) {
        forceNewline();
      }
      const frame = topFrame();
      frame.inCaseBody = false;
      beginToken(token, false);
      write(token.raw);
      pendingSpace = true;
      caseLabelFrame = frame;
      caseLabelActive = true;
      mark(token);
      prevCode = token;
      continue;
    }

    if (token.kind === "keyword") {
      if (value === "new") {
        newCallPending = true;
      } else if (value === "switch") {
        pendingSwitch = true;
      }
    }

    beginToken(token);
    if (token.kind === "string") {
      writeRawMultiline(token.raw);
    } else {
      write(token.raw);
    }
    pendingSpace = true;
    mark(token);
    prevCode = token;
  }

  forceNewline();
  while (outLines.length > 0 && outLines[outLines.length - 1] === "") {
    outLines.pop();
  }
  const body = outLines.join("\n");
  return body.length === 0 ? "\n" : body + "\n";
}
