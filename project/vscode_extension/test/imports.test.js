/*
 * Well 语言扩展：解析 / 整理导入 / 自动导入 的纯 node 单测。
 *
 * 编译（由任务约定）：
 *   npx tsc src/wellParse.ts src/imports.ts --outDir build/ts --module commonjs --target ES2020 --strict --skipLibCheck
 * 运行：
 *   node test/imports.test.js
 *
 * 测试文件只写 test/tmp（测试结束清理），不依赖 vscode 模块。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const { parseDeclarations, parseImports, parseModuleDecl, resolveImportSpec, importSpecFor } =
  require("../build/ts/wellParse.js");
const { organizeImports, addImport, findUnusedImports, importSpecExists } =
  require("../build/ts/imports.js");

const TMP = path.join(__dirname, "tmp");
const ROOT = path.join(__dirname, "..", "..");

// ---------------------------------------------------------------------------
// 迷你测试框架
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`ok - ${name}`);
  } catch (err) {
    failed += 1;
    failures.push({ name, err });
    console.error(`FAIL - ${name}`);
    console.error(err && err.stack ? err.stack : String(err));
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message || "assertion failed");
  }
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      `${message || "值不相等"}\n--- expected ---\n${formatValue(expected)}\n--- actual ---\n${formatValue(
        actual
      )}`
    );
  }
}

function assertDeepEqual(actual, expected, message) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) {
    throw new Error(`${message || "结构不相等"}\n--- expected ---\n${b}\n--- actual ---\n${a}`);
  }
}

function formatValue(value) {
  if (typeof value !== "string") {
    return JSON.stringify(value);
  }
  return JSON.stringify(value).replace(/\\r/g, "\\r").replace(/\\n/g, "\\n\n");
}

// ---------------------------------------------------------------------------
// 临时工作区
// ---------------------------------------------------------------------------

function writeFile(relPath, content) {
  const full = path.join(TMP, relPath);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content, "utf8");
}

function setupFixtures() {
  fs.rmSync(TMP, { recursive: true, force: true });
  fs.mkdirSync(path.join(TMP, "lib", "util"), { recursive: true });
  fs.mkdirSync(path.join(TMP, "app"), { recursive: true });
  fs.mkdirSync(path.join(TMP, "deep", "sub"), { recursive: true });

  writeFile(
    "main.we",
    ["module app.main;", "", "struct MainThing {", "    n: int;", "}", ""].join("\n")
  );
  writeFile(
    "lib/item.we",
    ["module app.item;", "", "struct ItemVo {", "    value: int;", "}", ""].join("\n")
  );
  writeFile(
    "lib/config.we",
    [
      "module app.config;",
      "",
      "static AppConfig {",
      '    name: String = "svc";',
      "}",
      "",
      "namespace AppUtil {",
      "    addr(): String {",
      "        return AppConfig.name;",
      "    }",
      "}",
      "",
    ].join("\n")
  );
  writeFile(
    "lib/unused.we",
    ["module app.unused;", "", "struct GhostThing {", "    x: int;", "}", ""].join("\n")
  );
  writeFile(
    "lib/thing.we",
    ["module app.thing;", "", "struct Thing {", "    n: int;", "}", ""].join("\n")
  );
  writeFile(
    "lib/util/index.we",
    ["module app.util;", "", "func utilHelper(): int {", "    return 1;", "}", ""].join("\n")
  );
  writeFile(
    "app/config.we",
    ["module app.cfg;", "", "struct DeepThing {", "    n: int;", "}", ""].join("\n")
  );
  writeFile(
    "index.we",
    ["module root.index;", "", "const RootConst: int = 0;", ""].join("\n")
  );
  writeFile(
    "thing.we",
    ["module root.thing;", "", "struct FallbackThing {", "    n: int;", "}", ""].join("\n")
  );
}

// 测试是同步注册、同步执行的：fixtures 必须在下面所有用例执行前就绪。
setupFixtures();

function fsImportContext(file) {
  return {
    file,
    declsOf: (absPath) => {
      try {
        return parseDeclarations(fs.readFileSync(absPath, "utf8")).map((decl) => decl.name);
      } catch {
        return null;
      }
    },
  };
}

function crlf(lines) {
  return lines.join("\r\n") + "\r\n";
}

function lf(lines) {
  return lines.join("\n") + "\n";
}

function assertCrlf(text, message) {
  assert(!/(^|[^\r])\n/.test(text), `${message || "应保持 CRLF"}：出现了裸 LF`);
}

// ---------------------------------------------------------------------------
// parseDeclarations
// ---------------------------------------------------------------------------

const DECL_SOURCE = `module app.main;

// struct FakeComment { a: int; }
/* func fakeBlock() {
     namespace FakeNs {
   }
} */
struct Vo1 {
    a: int;
}

event Ev1 {
    n: int;
}

controller Ctl1 extends Controller {
    run() {
        // handler FakeNested { }
    }
}

handler Hd1 extends Handler {
    onEvent(e: Ev1) {
    }
}

func fn1(a: int): int {
    var s: String = "enum FakeInString { X = 0; }";
    return a;
}

namespace Ns1 {
    add(): int {
        return 1;
    }
}

static St1 {
    x: int = 1;
}

enum En1 {
    RED = 0;
}

const Co1: int = 1;
`;

function lineOf(source, marker) {
  const lines = source.split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].startsWith(marker)) {
      return i + 1;
    }
  }
  throw new Error(`fixture 中找不到 ${marker}`);
}

test("parseDeclarations 识别 9 种顶层声明并记录行号", () => {
  const decls = parseDeclarations(DECL_SOURCE);
  assertDeepEqual(
    decls.map((decl) => [decl.name, decl.kind]),
    [
      ["Vo1", "struct"],
      ["Ev1", "event"],
      ["Ctl1", "controller"],
      ["Hd1", "handler"],
      ["fn1", "func"],
      ["Ns1", "namespace"],
      ["St1", "static"],
      ["En1", "enum"],
      ["Co1", "const"],
    ]
  );
  for (const decl of decls) {
    const keyword = decl.kind;
    assertEqual(decl.line, lineOf(DECL_SOURCE, `${keyword} ${decl.name}`), `${decl.name} 行号`);
  }
});

test("parseDeclarations 忽略注释/字符串里的假声明", () => {
  const names = parseDeclarations(DECL_SOURCE).map((decl) => decl.name);
  for (const fake of ["FakeComment", "FakeBlock", "FakeNs", "FakeNested", "FakeInString"]) {
    assert(!names.includes(fake), `不应识别假声明 ${fake}`);
  }
});

test("parseDeclarations 对空/畸形源码不崩溃", () => {
  assertDeepEqual(parseDeclarations(""), []);
  assertDeepEqual(parseDeclarations("garbage {{{ ]]]"), []);
  assertDeepEqual(parseDeclarations('"unterminated'), []);
  assertDeepEqual(parseDeclarations("/* unterminated"), []);
  // 结构体内部的同名“声明”不是顶层声明
  const nested = "struct A {\n    func notTop(): int { return 1; }\n}\n";
  assertDeepEqual(parseDeclarations(nested), [{ name: "A", kind: "struct", line: 1 }]);
});

// ---------------------------------------------------------------------------
// parseImports / parseModuleDecl
// ---------------------------------------------------------------------------

const IMPORT_SOURCE = `module app.main;

import "lib/item.we";
import lib.util;
import "config";

struct X {
    label: String = "import \\"nope\\";";
}
`;

test("parseImports 识别两种语法 + 行号 + offset（含分号）", () => {
  const imports = parseImports(IMPORT_SOURCE);
  assertDeepEqual(
    imports.map((info) => [info.spec, info.line]),
    [
      ["lib/item.we", 3],
      ["lib.util", 4],
      ["config", 5],
    ]
  );
  assertEqual(IMPORT_SOURCE.slice(imports[0].start, imports[0].end), 'import "lib/item.we";');
  assertEqual(IMPORT_SOURCE.slice(imports[1].start, imports[1].end), "import lib.util;");
  assertEqual(IMPORT_SOURCE.slice(imports[2].start, imports[2].end), 'import "config";');
});

test("parseImports 忽略注释/字符串/块内的假 import", () => {
  const source = [
    "module app.main;",
    '// import "comment.we";',
    '/* import "block.we"; */',
    "",
    'func f() {',
    '    print("import \\"string.we\\";");',
    '    import "nested.we";',
    "}",
    "",
    'import "real.we";',
  ].join("\n");
  assertDeepEqual(
    parseImports(source).map((info) => info.spec),
    ["real.we"]
  );
});

test("parseImports 对空/畸形源码不崩溃", () => {
  assertDeepEqual(parseImports(""), []);
  assertDeepEqual(parseImports('import "oops"'), []); // 缺分号
  assertDeepEqual(parseImports("import ;"), []);
});

test("parseModuleDecl 提取点路径与 offset", () => {
  const source = "// 头注释\nmodule demo.app.main;\n\nstruct X { a: int; }\n";
  const moduleInfo = parseModuleDecl(source);
  assert(moduleInfo !== null, "应识别 module");
  assertEqual(moduleInfo.name, "demo.app.main");
  assertEqual(moduleInfo.line, 2);
  assertEqual(source.slice(moduleInfo.start, moduleInfo.end), "module demo.app.main;");
});

test("parseModuleDecl 忽略注释/字符串中的假 module，缺失时返回 null", () => {
  assertEqual(parseModuleDecl('// module fake.x;\nstruct X { a: int; }'), null);
  assertEqual(parseModuleDecl('var s: String = "module fake.x;";'), null);
  assertEqual(parseModuleDecl(""), null);
});

// ---------------------------------------------------------------------------
// resolveImportSpec（对齐 compiler/wellc/driver.py::_resolve_import）
// ---------------------------------------------------------------------------

const MAIN_FILE = path.join(TMP, "main.we");
const ITEM_FILE = path.join(TMP, "lib", "item.we");
const CONFIG_FILE = path.join(TMP, "lib", "config.we");
const UTIL_INDEX_FILE = path.join(TMP, "lib", "util", "index.we");

test("resolveImportSpec：相对路径 / 带 .we / 省略 .we", () => {
  assertEqual(resolveImportSpec("lib/item.we", MAIN_FILE), path.resolve(ITEM_FILE));
  assertEqual(resolveImportSpec("lib/item", MAIN_FILE), path.resolve(ITEM_FILE));
  assertEqual(resolveImportSpec("./lib/item.we", MAIN_FILE), path.resolve(ITEM_FILE));
  assertEqual(resolveImportSpec("lib\\item.we", MAIN_FILE), path.resolve(ITEM_FILE));
});

test("resolveImportSpec：点路径 → a/b.we 与目录 index.we", () => {
  assertEqual(resolveImportSpec("lib.config", MAIN_FILE), path.resolve(CONFIG_FILE));
  assertEqual(resolveImportSpec("lib.util", MAIN_FILE), path.resolve(UTIL_INDEX_FILE));
  assertEqual(resolveImportSpec("app.config", MAIN_FILE), path.resolve(path.join(TMP, "app", "config.we")));
});

test("resolveImportSpec：同目录 basename 回退（driver 的第三种候选）", () => {
  // lib.missing.thing -> base/lib/missing/thing.we, .../index.we, base/thing.we
  assertEqual(resolveImportSpec("lib.missing.thing", MAIN_FILE), path.resolve(path.join(TMP, "thing.we")));
});

test("resolveImportSpec：不存在的 spec 返回 null", () => {
  assertEqual(resolveImportSpec("lib/nope.we", MAIN_FILE), null);
  assertEqual(resolveImportSpec("nope", MAIN_FILE), null);
  assertEqual(resolveImportSpec("nope.we", MAIN_FILE), null);
  assertEqual(resolveImportSpec("nope.sub", MAIN_FILE), null);
  // 目录存在但没有 index.we / 同名 .we
  fs.mkdirSync(path.join(TMP, "emptydir"), { recursive: true });
  assertEqual(resolveImportSpec("emptydir", MAIN_FILE), null);
});

test("resolveImportSpec：空 spec 与 driver 一样走点路径分支（base/index.we）", () => {
  assertEqual(resolveImportSpec("", MAIN_FILE), path.resolve(path.join(TMP, "index.we")));
  // deep/sub 下没有 index.we → null
  assertEqual(resolveImportSpec("", path.join(TMP, "deep", "sub", "main.we")), null);
});

test("resolveImportSpec：相对 ../ 与绝对路径", () => {
  const from = path.join(TMP, "deep", "sub", "main.we");
  assertEqual(resolveImportSpec("../../lib/item.we", from), path.resolve(ITEM_FILE));
  assertEqual(resolveImportSpec(ITEM_FILE, MAIN_FILE), path.resolve(ITEM_FILE));
});

// ---------------------------------------------------------------------------
// importSpecFor
// ---------------------------------------------------------------------------

test("importSpecFor：相对路径、正斜杠、无 ./ 前缀、往返可解析", () => {
  const spec = importSpecFor(MAIN_FILE, ITEM_FILE);
  assertEqual(spec, "lib/item.we");
  assertEqual(resolveImportSpec(spec, MAIN_FILE), path.resolve(ITEM_FILE));

  const up = importSpecFor(path.join(TMP, "lib", "other.we"), MAIN_FILE);
  assertEqual(up, "../main.we");
  assertEqual(resolveImportSpec(up, path.join(TMP, "lib", "other.we")), path.resolve(MAIN_FILE));

  const sameDir = importSpecFor(path.join(TMP, "lib", "other.we"), ITEM_FILE);
  assertEqual(sameDir, "item.we");
  assert(!sameDir.startsWith("./"), "不应有 ./ 前缀");

  const deep = importSpecFor(path.join(TMP, "deep", "sub", "main.we"), ITEM_FILE);
  assertEqual(deep, "../../lib/item.we");
  assertEqual(resolveImportSpec(deep, path.join(TMP, "deep", "sub", "main.we")), path.resolve(ITEM_FILE));
});

test("importSpecFor：自动补 .we 后缀", () => {
  const spec = importSpecFor(MAIN_FILE, path.join(TMP, "lib", "item"));
  assertEqual(spec, "lib/item.we");
  assertEqual(resolveImportSpec(spec, MAIN_FILE), path.resolve(ITEM_FILE));
});

test("importSpecFor：跨盘返回正斜杠绝对路径（仅 Windows）", () => {
  if (process.platform !== "win32") {
    return;
  }
  const spec = importSpecFor("C:\\work\\main.we", "D:\\libs\\item.we");
  assertEqual(spec, "D:/libs/item.we");
  assert(path.isAbsolute(spec.split("/").join(path.sep)), "跨盘时应为绝对路径");
});

// ---------------------------------------------------------------------------
// organizeImports
// ---------------------------------------------------------------------------

function contextForMain() {
  return fsImportContext(MAIN_FILE);
}

const ORGANIZE_INPUT = crlf([
  "module app.main;",
  "",
  'import "lib/thing.we";',
  'import "lib/config.we";',
  'import "lib/item.we";',
  "import lib.config;",
  "",
  "controller MainController extends Controller {",
  "    run() {",
  "        var item: ItemVo = new ItemVo();",
  "        print(item.value, AppConfig.name);",
  "    }",
  "}",
  "",
  "entry MainController;",
]);

const ORGANIZE_EXPECTED = crlf([
  "module app.main;",
  "",
  'import "lib/config.we";',
  'import "lib/item.we";',
  "",
  "controller MainController extends Controller {",
  "    run() {",
  "        var item: ItemVo = new ItemVo();",
  "        print(item.value, AppConfig.name);",
  "    }",
  "}",
  "",
  "entry MainController;",
]);

test("organizeImports：排序 + 去重（解析同一文件）+ 移除未使用", () => {
  const organized = organizeImports(ORGANIZE_INPUT, contextForMain());
  assertEqual(organized, ORGANIZE_EXPECTED);
  assertCrlf(organized);
});

test("organizeImports：幂等，无变化时严格返回原字符串", () => {
  const organized = organizeImports(ORGANIZE_INPUT, contextForMain());
  assertEqual(organizeImports(organized, contextForMain()), organized);
  assertEqual(organizeImports(ORGANIZE_EXPECTED, contextForMain()), ORGANIZE_EXPECTED);
});

test("organizeImports：LF 输入输出仍为 LF", () => {
  const input = ORGANIZE_INPUT.split("\r\n").join("\n");
  const expected = ORGANIZE_EXPECTED.split("\r\n").join("\n");
  const organized = organizeImports(input, contextForMain());
  assertEqual(organized, expected);
  assert(!organized.includes("\r"), "LF 文件不应出现 CR");
});

test("organizeImports：保留解析不到的 import（避免误删）", () => {
  const input = crlf([
    "module app.main;",
    "",
    'import "lib/missing.we";',
    'import "lib/item.we";',
    "",
    "struct UseIt {",
    "    item: ItemVo;",
    "}",
  ]);
  const expected = crlf([
    "module app.main;",
    "",
    'import "lib/item.we";',
    'import "lib/missing.we";',
    "",
    "struct UseIt {",
    "    item: ItemVo;",
    "}",
  ]);
  assertEqual(organizeImports(input, contextForMain()), expected);
});

test("organizeImports：declsOf 返回 null 时全部保留（仅排序/去重）", () => {
  const ctx = { file: MAIN_FILE, declsOf: () => null };
  const input = crlf([
    "module app.main;",
    "",
    'import "lib/thing.we";',
    'import "lib/item.we";',
    "",
    "struct X {",
    "    a: int;",
    "}",
  ]);
  const expected = crlf([
    "module app.main;",
    "",
    'import "lib/item.we";',
    'import "lib/thing.we";',
    "",
    "struct X {",
    "    a: int;",
    "}",
  ]);
  assertEqual(organizeImports(input, ctx), expected);
});

test("organizeImports：注释/字符串里的名字不算使用", () => {
  const input = crlf([
    "module app.main;",
    "",
    'import "lib/unused.we";',
    "",
    "func f() {",
    "    // GhostThing",
    '    print("GhostThing");',
    "}",
  ]);
  const organized = organizeImports(input, contextForMain());
  assertEqual(organized, crlf(["module app.main;", "", "func f() {", "    // GhostThing", '    print("GhostThing");', "}"]));
});

test("organizeImports：空行规范化（module 后、import 组后各一个空行）", () => {
  const input = crlf([
    "module app.main;",
    'import "lib/item.we";',
    "struct X {",
    "    a: ItemVo;",
    "}",
  ]);
  const expected = crlf([
    "module app.main;",
    "",
    'import "lib/item.we";',
    "",
    "struct X {",
    "    a: ItemVo;",
    "}",
  ]);
  assertEqual(organizeImports(input, contextForMain()), expected);
});

test("organizeImports：全部 import 被移除时模块后只留一个空行", () => {
  const input = crlf([
    "module app.main;",
    "",
    'import "lib/unused.we";',
    "",
    "struct X {",
    "    a: int;",
    "}",
  ]);
  const expected = crlf(["module app.main;", "", "struct X {", "    a: int;", "}"]);
  assertEqual(organizeImports(input, contextForMain()), expected);
});

test("organizeImports：无 module、import 全部移除时不留多余空行", () => {
  const input = crlf([
    'import "lib/unused.we";',
    "",
    "struct Y {",
    "    a: int;",
    "}",
  ]);
  const expected = crlf(["struct Y {", "    a: int;", "}"]);
  assertEqual(organizeImports(input, contextForMain()), expected);
});

test("organizeImports：保留 BOM、保留点路径写法、反斜杠规范成正斜杠", () => {
  const bomInput =
    "\uFEFF" +
    crlf([
      "module app.main;",
      "",
      'import "lib\\\\item.we";',
      "import lib.config;",
      "",
      "struct X {",
      "    a: ItemVo;",
      "    b: AppConfig;",
      "}",
    ]);
  const organized = organizeImports(bomInput, contextForMain());
  assert(organized.charCodeAt(0) === 0xfeff, "应保留 BOM");
  assertEqual(organized.split("\uFEFF").length - 1, 1, "BOM 只能出现一次");
  assert(organized.includes('import "lib/item.we";'), "反斜杠应规范成正斜杠");
  assert(organized.includes("import lib.config;"), "点路径写法应保留");
});

test("organizeImports：无 import / 空源码 / 畸形源码时原样返回", () => {
  const noImports = crlf(["module app.main;", "", "struct X {", "    a: int;", "}"]);
  assertEqual(organizeImports(noImports, contextForMain()), noImports);
  assertEqual(organizeImports("", contextForMain()), "");
  assertEqual(organizeImports("garbage {{{", contextForMain()), "garbage {{{");
  assertEqual(organizeImports("\uFEFF", contextForMain()), "\uFEFF");
});

test("organizeImports：只整理 import 块之后的正文逐字节不变", () => {
  const body = crlf([
    "controller MainController extends Controller {",
    "    run() {",
    "        var item: ItemVo = new ItemVo();   // 行尾注释保留",
    "    }",
    "}",
    "",
    "entry MainController;",
  ]);
  const input =
    crlf(["module app.main;", "", 'import "lib/thing.we";', 'import "lib/item.we";']) + "\r\n" + body;
  const organized = organizeImports(input, contextForMain());
  assert(organized.endsWith(body), "import 块之后的正文必须逐字节保留");
});

test("organizeImports：注释中的假 import 不会被当成真的 import", () => {
  const input = crlf([
    "module app.main;",
    "",
    '// import "lib/unused.we";',
    'import "lib/item.we";',
    "",
    "struct X {",
    "    a: ItemVo;",
    "}",
  ]);
  const organized = organizeImports(input, contextForMain());
  assertEqual(organized, input, "只有一条真实 import 且已使用，应原样返回");
});

test("organizeImports：import 之间的注释也会保留（不会当 import 解析）", () => {
  const input = crlf([
    "module app.main;",
    "",
    'import "lib/item.we";',
    "// note: config 必须保留",
    'import "lib/config.we";',
    "",
    "struct X {",
    "    a: ItemVo;",
    "    b: AppConfig;",
    "}",
  ]);
  const expected = crlf([
    "module app.main;",
    "",
    "// note: config 必须保留",
    'import "lib/config.we";',
    'import "lib/item.we";',
    "",
    "struct X {",
    "    a: ItemVo;",
    "    b: AppConfig;",
    "}",
  ]);
  const organized = organizeImports(input, contextForMain());
  assertEqual(organized, expected);
  assertEqual(organizeImports(organized, contextForMain()), organized, "整理结果应幂等");
});

// ---------------------------------------------------------------------------
// addImport
// ---------------------------------------------------------------------------

test("addImport：有 module 无 import 时插到 module 之后，组后空一行", () => {
  const input = crlf(["module app.main;", "", "struct X {", "    a: int;", "}"]);
  const expected = crlf([
    "module app.main;",
    "",
    'import "lib/item.we";',
    "",
    "struct X {",
    "    a: int;",
    "}",
  ]);
  assertEqual(addImport(input, contextForMain(), "lib/item.we"), expected);
});

test("addImport：已有 import 时追加到导入组末尾", () => {
  const input = crlf(["module app.main;", "", 'import "lib/thing.we";', "", "struct X {", "    a: int;", "}"]);
  const expected = crlf([
    "module app.main;",
    "",
    'import "lib/thing.we";',
    'import "lib/item.we";',
    "",
    "struct X {",
    "    a: int;",
    "}",
  ]);
  assertEqual(addImport(input, contextForMain(), "lib/item.we"), expected);
});

test("addImport：已存在相同 spec 时原样返回", () => {
  const input = crlf(["module app.main;", "", 'import "lib/item.we";', "", "struct X {", "    a: int;", "}"]);
  assertEqual(addImport(input, contextForMain(), "lib/item.we"), input);
});

test("addImport：已存在解析到同一文件的 import 时原样返回", () => {
  const input = crlf(["module app.main;", "", "import lib.item;", "", "struct X {", "    a: int;", "}"]);
  assertEqual(addImport(input, contextForMain(), "lib/item.we"), input);
});

test("addImport：无 module 时插到文件顶部", () => {
  const input = crlf(["struct X {", "    a: int;", "}"]);
  const expected = crlf(['import "lib/item.we";', "", "struct X {", "    a: int;", "}"]);
  assertEqual(addImport(input, contextForMain(), "lib/item.we"), expected);
});

test("addImport：保留 CRLF 与 BOM", () => {
  const input = "\uFEFF" + crlf(["module app.main;", "", "struct X {", "    a: int;", "}"]);
  const added = addImport(input, contextForMain(), "lib/item.we");
  assert(added.charCodeAt(0) === 0xfeff, "应保留 BOM");
  assertCrlf(added);
  assert(added.split("\uFEFF").length - 1 === 1, "BOM 只能出现一次");
  assert(added.includes('import "lib/item.we";'), "应包含新增 import");
});

test("addImport：点路径 spec 用 import a.b; 写法", () => {
  const input = lf(["module app.main;", "", "struct X {", "    a: int;", "}"]);
  const added = addImport(input, contextForMain(), "lib.util");
  assert(added.includes("import lib.util;"), `应使用点路径写法，实际：${added}`);
});

test("addImport：module 行后没有换行时也能插入", () => {
  const input = "module app.main;";
  const added = addImport(input, contextForMain(), "lib/item.we");
  assertEqual(added, "module app.main;\n\nimport \"lib/item.we\";\n");
});

test("addImport 结果可被 organizeImports 稳定接受", () => {
  const input = crlf([
    "module app.main;",
    "",
    "struct X {",
    "    a: ItemVo;",
    "}",
  ]);
  const added = addImport(input, contextForMain(), "lib/item.we");
  assertEqual(organizeImports(added, contextForMain()), added);
});

test("addImport：空 spec 不修改源码", () => {
  const input = crlf(["module app.main;", "", "struct X {", "    a: int;", "}"]);
  assertEqual(addImport(input, contextForMain(), "   "), input);
});

// ---------------------------------------------------------------------------
// findUnusedImports / importSpecExists
// ---------------------------------------------------------------------------

test("findUnusedImports：只返回确定未使用的 import（含 offset）", () => {
  const input = crlf([
    "module app.main;",
    "",
    'import "lib/item.we";',
    'import "lib/unused.we";',
    'import "lib/missing.we";',
    "",
    "struct UseIt {",
    "    item: ItemVo;",
    "}",
  ]);
  const unused = findUnusedImports(input, contextForMain());
  assertDeepEqual(
    unused.map((info) => info.spec),
    ["lib/unused.we"]
  );
  assertEqual(input.slice(unused[0].start, unused[0].end), 'import "lib/unused.we";');
  assertEqual(unused[0].line, 4);
});

test("findUnusedImports：无 import / 全部使用时返回空数组", () => {
  assertDeepEqual(findUnusedImports("module app.main;\n", contextForMain()), []);
  const all = crlf([
    "module app.main;",
    "",
    'import "lib/item.we";',
    "",
    "struct UseIt {",
    "    item: ItemVo;",
    "}",
  ]);
  assertDeepEqual(findUnusedImports(all, contextForMain()), []);
});

test("importSpecExists：文本等价（.we / ./ / 反斜杠）判定", () => {
  const source = crlf(["module app.main;", "", 'import "lib/item.we";']);
  assert(importSpecExists(source, "lib/item.we"));
  assert(importSpecExists(source, "lib/item"));
  assert(importSpecExists(source, "./lib/item.we"));
  assert(importSpecExists(source, "lib\\item.we"));
  assert(!importSpecExists(source, "lib/other.we"));
  assert(!importSpecExists(source, ""));
});

test("importSpecExists：点路径与注释里的假 import", () => {
  const source = crlf(["module app.main;", "", "import lib.util;", '// import "lib/nope.we";']);
  assert(importSpecExists(source, "lib.util"));
  assert(!importSpecExists(source, "lib/nope.we"));
  assert(!importSpecExists(source, "lib/util.we"));
});

// ---------------------------------------------------------------------------
// 真实语料回归（项目内 compiler/ 下的 .we 文件）
// ---------------------------------------------------------------------------

test("真实语料：15_import 用例的 import 解析与 driver 语义一致", () => {
  const caseDir = path.join(ROOT, "compiler", "tests", "cases", "15_import");
  const mainFile = path.join(caseDir, "main.we");
  if (!fs.existsSync(mainFile)) {
    return;
  }
  const source = fs.readFileSync(mainFile, "utf8");
  const imported = parseImports(source);
  assertDeepEqual(
    imported.map((info) => info.spec),
    ["lib/item.we"]
  );
  assertEqual(source.slice(imported[0].start, imported[0].end), 'import "lib/item.we";');
  assertEqual(parseModuleDecl(source).name, "app.main");
  assertEqual(resolveImportSpec("lib/item.we", mainFile), path.join(caseDir, "lib", "item.we"));
  // 15_import/lib/item.we 的顶层声明
  assertDeepEqual(
    parseDeclarations(fs.readFileSync(path.join(caseDir, "lib", "item.we"), "utf8")).map(
      (decl) => [decl.name, decl.kind]
    ),
    [["ItemVo", "struct"]]
  );
});

test("真实语料：75_static_import / http_parser 示例的 import 解析", () => {
  const staticDir = path.join(ROOT, "compiler", "tests", "cases", "75_static_import");
  const staticMain = path.join(staticDir, "main.we");
  if (fs.existsSync(staticMain)) {
    const source = fs.readFileSync(staticMain, "utf8");
    assertDeepEqual(
      parseImports(source).map((info) => info.spec),
      ["config"]
    );
    assertEqual(resolveImportSpec("config", staticMain), path.join(staticDir, "config.we"));
    assertDeepEqual(
      parseDeclarations(fs.readFileSync(path.join(staticDir, "config.we"), "utf8")).map(
        (decl) => [decl.name, decl.kind]
      ),
      [
        ["AppConfig", "static"],
        ["AppUtil", "namespace"],
      ]
    );
  }

  const bench = path.join(ROOT, "compiler", "examples", "http_parser_bench", "main.we");
  if (fs.existsSync(bench)) {
    const source = fs.readFileSync(bench, "utf8");
    assertDeepEqual(
      parseImports(source).map((info) => info.spec),
      ["../lib/http_parser"]
    );
    assertEqual(
      resolveImportSpec("../lib/http_parser", bench),
      path.join(ROOT, "compiler", "examples", "lib", "http_parser.we")
    );
  }
});

test("真实语料：examples/lib/http_parser.we 的顶层声明", () => {
  const file = path.join(ROOT, "compiler", "examples", "lib", "http_parser.we");
  if (!fs.existsSync(file)) {
    return;
  }
  const decls = parseDeclarations(fs.readFileSync(file, "utf8"));
  assertDeepEqual(
    decls.map((decl) => [decl.name, decl.kind]),
    [
      ["ParsedReq", "struct"],
      ["parseUnsigned", "func"],
      ["parseRequest", "func"],
    ]
  );
});

test("真实语料：所有测试用例 main.we 都能被无异常扫描", () => {
  const casesDir = path.join(ROOT, "compiler", "tests", "cases");
  if (!fs.existsSync(casesDir)) {
    return;
  }
  let count = 0;
  for (const name of fs.readdirSync(casesDir)) {
    const mainFile = path.join(casesDir, name, "main.we");
    if (!fs.existsSync(mainFile)) {
      continue;
    }
    const source = fs.readFileSync(mainFile, "utf8");
    const decls = parseDeclarations(source);
    assert(Array.isArray(decls), `${name}: parseDeclarations 应返回数组`);
    for (const decl of decls) {
      assert(typeof decl.name === "string" && decl.name.length > 0, `${name}: 声明名非法`);
      assert(decl.line >= 1, `${name}: 行号应为 1-based`);
    }
    for (const info of parseImports(source)) {
      assert(source.slice(info.start, info.end).includes("import"), `${name}: import offset 非法`);
    }
    count += 1;
  }
  assert(count > 50, `应扫描到足够多的真实用例，实际 ${count}`);
});

// ---------------------------------------------------------------------------
// 收尾
// ---------------------------------------------------------------------------

fs.rmSync(TMP, { recursive: true, force: true });

console.log("");
console.log(`${passed} passed, ${failed} failed`);
if (failed > 0) {
  for (const failure of failures) {
    console.error(`- ${failure.name}`);
  }
  process.exit(1);
}
