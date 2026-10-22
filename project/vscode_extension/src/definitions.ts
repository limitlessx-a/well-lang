/**
 * Definition scanning helpers: top-level declarations, struct/event fields,
 * enum members, `extends` bases and best-effort local variable types.
 *
 * Pure TypeScript (no `vscode` import) so it can be unit-tested with node and
 * reused by the go-to-definition / import-path resolution logic.
 */
import {
  DeclInfo,
  DeclKind,
  ImportInfo,
  parseDeclarations,
  parseImports,
  scanWellTokens,
  WellScanToken,
} from "./wellParse";

export interface FieldInfo {
  /** struct / event that declares the field. */
  owner: string;
  ownerKind: "struct" | "event";
  name: string;
  /** 简单类型名（泛型只保留外层名字，如 `Array`）。 */
  type: string;
  /** 1-based line. */
  line: number;
}

export interface EnumMemberInfo {
  enumName: string;
  name: string;
  /** 1-based line. */
  line: number;
}

export interface ParsedFileInfo {
  decls: DeclInfo[];
  imports: ImportInfo[];
  fields: FieldInfo[];
  enumMembers: EnumMemberInfo[];
  /** struct/event 名 -> `extends` 的基类名。 */
  bases: Map<string, string>;
  /** 标识符 -> 简单类型名（var 声明 / 参数 / 字段），用于 `obj.field` 跳转。 */
  varTypes: Map<string, string>;
}

const DECL_KINDS: Record<string, DeclKind> = {
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

const PRIMITIVE_NAMES = new Set([
  "int",
  "long",
  "float",
  "double",
  "bool",
  "String",
  "Bytes",
  "void",
  "null",
]);

/** 这些名字出现在 `name: Type` 位置时不是变量/字段声明。 */
const NON_VARIABLE_NAMES = new Set([
  "case",
  "default",
  "of",
  "in",
  "return",
  "break",
  "continue",
  "if",
  "else",
  "while",
  "for",
  "switch",
  "var",
  "new",
  "true",
  "false",
  "null",
  "void",
  "bool",
  "int",
  "long",
  "float",
  "double",
  "String",
  "Bytes",
  "Array",
  "Map",
  "Linked",
  "extends",
  "module",
  "import",
  "entry",
  "const",
  "enum",
  "struct",
  "event",
  "controller",
  "handler",
  "func",
  "namespace",
  "static",
]);

interface BodyContext {
  name: string;
  kind: "struct" | "event" | "enum";
}

function isOp(token: WellScanToken | undefined, value: string): boolean {
  return token !== undefined && token.kind === "punct" && token.value === value;
}

function isWord(token: WellScanToken | undefined): token is WellScanToken {
  return token !== undefined && token.kind === "word";
}

/** Scan a `.we` file for the pieces needed by go-to-definition. */
export function parseFileInfo(source: string): ParsedFileInfo {
  const info: ParsedFileInfo = {
    decls: parseDeclarations(source),
    imports: parseImports(source),
    fields: [],
    enumMembers: [],
    bases: new Map(),
    varTypes: new Map(),
  };
  if (!source) {
    return info;
  }
  const tokens = scanWellTokens(source);
  let depth = 0;
  let body: BodyContext | undefined;
  let pendingBody: BodyContext | undefined;

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];

    if (isOp(token, "{")) {
      if (depth === 0 && pendingBody) {
        body = pendingBody;
        pendingBody = undefined;
      }
      depth += 1;
      continue;
    }
    if (isOp(token, "}")) {
      if (depth === 1 && body) {
        body = undefined;
      }
      depth = Math.max(0, depth - 1);
      continue;
    }
    if (token.kind !== "word") {
      continue;
    }

    const declKind = DECL_KINDS[token.value];
    if (declKind && depth === 0) {
      const nameToken = tokens[i + 1];
      if (isWord(nameToken)) {
        if (declKind === "struct" || declKind === "event" || declKind === "enum") {
          pendingBody = { name: nameToken.value, kind: declKind };
        }
        if (declKind === "struct" || declKind === "event") {
          // 找 `struct A extends B {` 中的 B（最多向后看 8 个 token）
          for (let k = i + 2; k < tokens.length && k < i + 8; k += 1) {
            const probe = tokens[k];
            if (isOp(probe, "{")) {
              break;
            }
            if (isWord(probe) && probe.value === "extends") {
              const base = tokens[k + 1];
              if (isWord(base)) {
                info.bases.set(nameToken.value, base.value);
              }
              break;
            }
          }
        }
        i += 1;
        continue;
      }
    }

    // enum 成员：enum 体内 depth 1，`MEMBER;` 或 `MEMBER = ...;`
    if (body && body.kind === "enum" && depth === 1) {
      const next = tokens[i + 1];
      const isMemberTail = isOp(next, ";") || isOp(next, "=");
      if (isMemberTail && !NON_VARIABLE_NAMES.has(token.value)) {
        info.enumMembers.push({
          enumName: body.name,
          name: token.value,
          line: token.line,
        });
        continue;
      }
    }

    // `name: Type`（var / 参数 / struct 字段），用于 obj.field 的类型推断。
    const prev = tokens[i - 1];
    const colon = tokens[i + 1];
    const typeToken = tokens[i + 2];
    const allowedPrev =
      prev !== undefined &&
      (isOp(prev, "(") ||
        isOp(prev, ",") ||
        isOp(prev, ";") ||
        isOp(prev, "{") ||
        (prev.kind === "word" && prev.value === "var"));
    if (
      allowedPrev &&
      isOp(colon, ":") &&
      isWord(typeToken) &&
      !NON_VARIABLE_NAMES.has(token.value)
    ) {
      if (body && depth === 1 && (body.kind === "struct" || body.kind === "event")) {
        info.fields.push({
          owner: body.name,
          ownerKind: body.kind,
          name: token.value,
          type: typeToken.value,
          line: token.line,
        });
      }
      if (!info.varTypes.has(token.value)) {
        info.varTypes.set(token.value, typeToken.value);
      }
    }
  }

  // 字段类型也登记到 varTypes，方便 `a.b.c` 的多级跳转（b 是 A 的字段）。
  for (const field of info.fields) {
    if (!info.varTypes.has(field.name)) {
      info.varTypes.set(field.name, field.type);
    }
  }
  return info;
}

/**
 * 简单类型名：去掉泛型参数外层（`Array<ItemVo>` -> `Array`，`ItemVo[]` -> `ItemVo`）。
 */
export function simpleTypeName(raw: string): string | undefined {
  const match = /^[A-Za-z_][A-Za-z0-9_]*/.exec(raw);
  return match ? match[0] : undefined;
}

/** 该类型名是否可能是自定义 struct/event/enum（排除内建类型与关键字）。 */
export function isUserTypeName(name: string | undefined): name is string {
  if (!name) {
    return false;
  }
  return !NON_VARIABLE_NAMES.has(name) && !PRIMITIVE_NAMES.has(name);
}
