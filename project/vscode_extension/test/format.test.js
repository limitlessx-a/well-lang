#!/usr/bin/env node
/*
 * Well 格式化器单元测试（纯 node，无第三方依赖）。
 *
 * 运行方式（在 project/vscode_extension 下）：
 *   npx tsc src/formatter.ts --outDir build/ts --module commonjs --target ES2020 --strict --skipLibCheck
 *   node test/format.test.js
 *
 * 覆盖：
 *  1) 自动遍历 project/compiler 下所有真实 .we 文件，逐文件断言
 *     token 序列不变、幂等、LF 结尾、无行尾空白、无 tab（insertSpaces=true）。
 *  2) 手写边界用例：字符串里的 `//`、for 头不换行、switch case 缩进、块注释保留、
 *     空块 `{}`、行注释贴尾、CRLF、未闭合花括号、泛型尖括号、三目/??/复合赋值、
 *     事件初始化/映射字面量、tab 缩进选项、空输入等。
 *
 * 任一律失败时退出码为 1，并在末尾打印失败明细。
 */
'use strict';

const fs = require('fs');
const path = require('path');

const BUILD_FILE = path.join(__dirname, '..', 'build', 'ts', 'formatter.js');
if (!fs.existsSync(BUILD_FILE)) {
  console.error('[format.test] 缺少编译产物: ' + BUILD_FILE);
  console.error('请先运行: npx tsc src/formatter.ts --outDir build/ts --module commonjs --target ES2020 --strict --skipLibCheck');
  process.exit(1);
}

const formatter = require(BUILD_FILE);
const formatWell = formatter.formatWell;
const tokenizeWell = formatter.tokenizeWell;

if (typeof formatWell !== 'function' || typeof tokenizeWell !== 'function') {
  console.error('[format.test] build/ts/formatter.js 未导出 formatWell/tokenizeWell');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// 断言与计数工具
// ---------------------------------------------------------------------------

const failures = [];
const stats = {
  real: { pass: 0, fail: 0 },
  unit: { pass: 0, fail: 0 },
};

function test(group, name, fn) {
  try {
    fn();
    stats[group].pass += 1;
  } catch (err) {
    stats[group].fail += 1;
    failures.push({ group: group, name: name, error: err instanceof Error ? err : new Error(String(err)) });
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      message +
      '\n    期望: ' + JSON.stringify(expected) +
      '\n    实际: ' + JSON.stringify(actual),
    );
  }
}

function tokenBrief(token) {
  return JSON.stringify({ kind: token.kind, value: token.value, raw: token.raw });
}

/** 断言格式化前后 token 序列（kind/value/raw，含注释）完全一致。 */
function assertTokensPreserved(source, formatted, label) {
  const before = tokenizeWell(source);
  const after = tokenizeWell(formatted);
  if (before.length !== after.length) {
    throw new Error(label + ': token 数量变化 ' + before.length + ' -> ' + after.length);
  }
  for (let i = 0; i < before.length; i += 1) {
    const a = before[i];
    const b = after[i];
    if (a.kind !== b.kind || a.value !== b.value || a.raw !== b.raw) {
      throw new Error(label + ': 第 ' + i + ' 个 token 变化\n    before: ' + tokenBrief(a) + '\n    after : ' + tokenBrief(b));
    }
  }
}

/** 断言输出满足统一换行/无行尾空白/无 tab 等硬性要求。 */
function assertClean(formatted, label, options) {
  const opts = options || {};
  const expectSpaces = opts.insertSpaces !== false;
  if (!formatted.endsWith('\n')) {
    throw new Error(label + ': 文件末尾缺少换行符');
  }
  if (formatted.endsWith('\n\n')) {
    throw new Error(label + ': 文件末尾存在多个换行（应恰好一个）');
  }
  if (formatted.indexOf('\r') !== -1) {
    throw new Error(label + ': 输出仍包含 CR（应为 LF）');
  }
  const lines = formatted.slice(0, -1).split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    if (/[ \t]+$/.test(lines[i])) {
      throw new Error(label + ': 第 ' + (i + 1) + ' 行存在行尾空白: ' + JSON.stringify(lines[i]));
    }
    if (expectSpaces && lines[i].indexOf('\t') !== -1) {
      throw new Error(label + ': 第 ' + (i + 1) + ' 行包含 tab: ' + JSON.stringify(lines[i]));
    }
  }
}

// ---------------------------------------------------------------------------
// 真实 .we 文件发现
// ---------------------------------------------------------------------------

const compilerRoot = path.join(__dirname, '..', '..', 'compiler');
const SKIP_DIRS = new Set(['node_modules', 'build', 'out', '.git', '.svn', 'tmp']);

function collectWellFiles(dir) {
  const result = [];
  if (!fs.existsSync(dir)) {
    return result;
  }
  const entries = fs.readdirSync(dir);
  for (const name of entries) {
    const full = path.join(dir, name);
    let stat;
    try {
      stat = fs.statSync(full);
    } catch (err) {
      continue;
    }
    if (stat.isDirectory()) {
      if (SKIP_DIRS.has(name)) {
        continue;
      }
      for (const nested of collectWellFiles(full)) {
        result.push(nested);
      }
    } else if (stat.isFile() && name.endsWith('.we')) {
      result.push(full);
    }
  }
  return result;
}

const realFiles = collectWellFiles(compilerRoot).sort();

// ---------------------------------------------------------------------------
// 1) 真实 .we 文件：逐个断言
// ---------------------------------------------------------------------------

for (const file of realFiles) {
  const relative = path.relative(path.join(__dirname, '..', '..'), file).split(path.sep).join('/');
  test('real', relative, function () {
    const source = fs.readFileSync(file, 'utf8');
    const formatted = formatWell(source);
    assertTokensPreserved(source, formatted, 'token 守恒');
    assertClean(formatted, '格式');
    assertEqual(formatWell(formatted), formatted, '幂等');
  });
}

// 找不到任何真实样例时也要显式失败，避免路径问题导致“空跑通过”。
test('real', '<真实 .we 文件发现>', function () {
  assert(realFiles.length > 0, '在 ' + compilerRoot + ' 下未发现任何 .we 文件');
});

// ---------------------------------------------------------------------------
// 2) 手写边界用例
// ---------------------------------------------------------------------------

const UNIT = 'unit';

test(UNIT, 'tokenize: 保留注释、字符串中的 // 与 /* 不误判为注释', function () {
  const source = 'var u: String = "http://a//b/*c*/"; // real\n/* block */\n';
  const tokens = tokenizeWell(source);
  const comments = tokens.filter(function (t) { return t.kind === 'comment'; });
  assertEqual(comments.length, 2, '注释 token 数量');
  assertEqual(comments[0].raw, '// real', '行注释原文保留');
  assertEqual(comments[1].raw, '/* block */', '块注释原文保留');

  const strings = tokens.filter(function (t) { return t.kind === 'string'; });
  assertEqual(strings.length, 1, '字符串 token 数量');
  assertEqual(strings[0].raw, '"http://a//b/*c*/"', '字符串 raw 保留引号与内容');
  assertEqual(strings[0].value, 'http://a//b/*c*/', '字符串 value 为去引号语义值');

  const eof = tokens[tokens.length - 1];
  assertEqual(eof.kind, 'eof', '末尾应有 eof token');
});

test(UNIT, 'tokenize: 关键字/标识符/数字/运算符分类与 line/col', function () {
  const tokens = tokenizeWell('var x: int = 1;');
  const kinds = tokens.map(function (t) { return t.kind; });
  assertEqual(kinds.join(','), 'keyword,ident,op,keyword,op,number,op,eof', 'token kind 序列');
  assertEqual(tokens[0].value, 'var', 'keyword value');
  assertEqual(tokens[1].value, 'x', 'ident value');
  assertEqual(tokens[2].value, ':', 'op value');
  assertEqual(tokens[3].value, 'int', 'keyword 类型名');
  assertEqual(tokens[5].value, '1', 'number value');

  const positioned = tokenizeWell('a\n  b');
  assertEqual(positioned[0].line, 1, '首个 token 行号');
  assertEqual(positioned[0].col, 1, '首个 token 列号');
  assertEqual(positioned[1].line, 2, '第二行 token 行号');
  assertEqual(positioned[1].col, 3, '第二行 token 列号');
});

test(UNIT, '字符串中的 // 不当作注释（格式化后仍是字符串）', function () {
  const source = 'var u: String = "http://example.com/a//b";\n';
  const formatted = formatWell(source);
  assertEqual(formatted, 'var u: String = "http://example.com/a//b";\n', 'URL 字符串原样保留');
  assertEqual(tokenizeWell(formatted).filter(function (t) { return t.kind === 'comment'; }).length, 0, '不应产生注释 token');
});

test(UNIT, 'for 头内的分号/表达式不换行', function () {
  const source = 'for(var i:int=0;i<3;i=i+1){print(i);}\n';
  const formatted = formatWell(source);
  assertEqual(
    formatted,
    'for (var i: int = 0; i < 3; i = i + 1) {\n    print(i);\n}\n',
    'for 头保持单行',
  );
});

test(UNIT, 'if/else：块缩进、`} else {` 同行', function () {
  const source = 'if(a){b();}else{c();}\n';
  const formatted = formatWell(source);
  assertEqual(
    formatted,
    'if (a) {\n    b();\n} else {\n    c();\n}\n',
    'if/else 排版',
  );
});

test(UNIT, 'switch/case/default 缩进', function () {
  const source = [
    'module demo.t;',
    '',
    'controller C extends Controller {',
    'run() {',
    'switch (x) {',
    'case 1: print("one");',
    'case 2: print("two"); break;',
    'default: print("def");',
    '}',
    '}',
    '}',
    '',
  ].join('\n');
  const expected = [
    'module demo.t;',
    '',
    'controller C extends Controller {',
    '    run() {',
    '        switch (x) {',
    '            case 1:',
    '                print("one");',
    '            case 2:',
    '                print("two");',
    '                break;',
    '            default:',
    '                print("def");',
    '        }',
    '    }',
    '}',
    '',
  ].join('\n');
  const formatted = formatWell(source);
  assertEqual(formatted, expected, 'switch case 缩进');
  assertEqual(formatWell(formatted), formatted, '幂等');
});

test(UNIT, '空块 {} 与空映射字面量 {} 保持一行', function () {
  const source = 'if (x) {}\nvar m: Map<int> = {};\n';
  const formatted = formatWell(source);
  assertEqual(formatted, 'if (x) {}\nvar m: Map<int> = {};\n', '空块/空字面量');
  assertEqual(formatWell(formatted), formatted, '幂等');
});

test(UNIT, '块注释原文保留且独占行', function () {
  const source = '/* keep   me */\nfunc f() {\n    return 1;\n}\n';
  const formatted = formatWell(source);
  assertEqual(formatted, source, '块注释独占行、内部空白保留');
  assertTokensPreserved(source, formatted, '块注释 token 守恒');
});

test(UNIT, '行注释跟在代码后保留在行尾', function () {
  const source = 'var a: int = 1;// note\nprint(a); // done\n';
  const formatted = formatWell(source);
  assertEqual(formatted, 'var a: int = 1; // note\nprint(a); // done\n', '行尾注释排版');
});

test(UNIT, 'CRLF 输入统一为 LF', function () {
  const source = 'module a;\r\n\r\ncontroller C extends Controller {\r\n    run() {\r\n        print("x");\r\n    }\r\n}\r\n';
  const formatted = formatWell(source);
  assertEqual(
    formatted,
    'module a;\n\ncontroller C extends Controller {\n    run() {\n        print("x");\n    }\n}\n',
    'CRLF -> LF',
  );
  assertEqual(formatted.indexOf('\r'), -1, '不应残留 CR');
  assertTokensPreserved(source, formatted, 'CRLF token 守恒');
});

test(UNIT, '未闭合花括号不崩溃且输出确定', function () {
  const source = 'controller C extends Controller {\nrun() {\nprint("x");\n';
  const first = formatWell(source);
  const second = formatWell(source);
  assertEqual(first, 'controller C extends Controller {\n    run() {\n        print("x");\n', '未闭合块输出');
  assertEqual(first, second, '同一输入输出确定');
  assertEqual(formatWell(first), first, '幂等');
});

test(UNIT, '不配对/乱序符号不崩溃', function () {
  const source = '}{);\n';
  const formatted = formatWell(source);
  assertEqual(formatWell(formatted), formatted, '幂等');
  assertTokensPreserved(source, formatted, 'token 守恒');
});

test(UNIT, '三目、??、复合赋值两侧空格', function () {
  const source = 'var a: int = p ?? q;\nvar b: int = p ? q : r;\nvar c: int = 1;c += 2;c -= 3;\n';
  const formatted = formatWell(source);
  assertEqual(
    formatted,
    'var a: int = p ?? q;\nvar b: int = p ? q : r;\nvar c: int = 1;\nc += 2;\nc -= 3;\n',
    '三目/??/复合赋值',
  );
});

test(UNIT, 'enum / 事件初始化 / 映射字面量', function () {
  const source = 'enum Color { RED = "red"; }\nvar ev: PingEvent = new PingEvent(7) { n: 1 };\nvar m: Map<int> = { "k": 1, "j": 2 };\n';
  const formatted = formatWell(source);
  assertEqual(
    formatted,
    'enum Color {\n    RED = "red";\n}\nvar ev: PingEvent = new PingEvent(7) { n: 1 };\nvar m: Map<int> = { "k": 1, "j": 2 };\n',
    'enum/事件初始化/映射字面量',
  );
});

test(UNIT, '泛型尖括号不空格、比较运算两侧空格', function () {
  const source = 'var a: Array<int> = [];\nvar m: Map<String> = {};\nif (a.length < 3 && m.size >= 1) {}\n';
  const formatted = formatWell(source);
  assertEqual(
    formatted,
    'var a: Array<int> = [];\nvar m: Map<String> = {};\nif (a.length < 3 && m.size >= 1) {}\n',
    '泛型/比较运算',
  );
});

test(UNIT, '一元负号与逻辑取反', function () {
  const source = 'var x: int = -1;\nvar y: bool = !a;\nvar z: int = a - -1;\nvar w: int = -a + 1;\n';
  const formatted = formatWell(source);
  assertEqual(
    formatted,
    'var x: int = -1;\nvar y: bool = !a;\nvar z: int = a - -1;\nvar w: int = -a + 1;\n',
    '一元运算符',
  );
});

test(UNIT, '连续空行压成 1 个，块首/块尾不留空行', function () {
  const source = 'module a;\n\n\nfunc f() {\n\n    return 1;\n\n}\n\n\n';
  const formatted = formatWell(source);
  assertEqual(formatted, 'module a;\n\nfunc f() {\n    return 1;\n}\n', '空行压缩');
});

test(UNIT, 'insertSpaces=false 使用 tab 缩进', function () {
  const source = 'controller C extends Controller {\nrun() {\nprint("x");\n}\n}\n';
  const formatted = formatWell(source, { insertSpaces: false });
  assertEqual(
    formatted,
    'controller C extends Controller {\n\trun() {\n\t\tprint("x");\n\t}\n}\n',
    'tab 缩进',
  );
});

test(UNIT, 'tabSize=2 使用 2 空格缩进', function () {
  const source = 'run() {\nprint(1);\n}\n';
  const formatted = formatWell(source, { tabSize: 2 });
  assertEqual(formatted, 'run() {\n  print(1);\n}\n', 'tabSize=2');
});

test(UNIT, '空输入/纯空白输入返回单个换行', function () {
  assertEqual(formatWell(''), '\n', '空输入');
  assertEqual(formatWell('   \n\n'), '\n', '纯空白输入');
  assertTokensPreserved(' \t\n\n ', formatWell('   \n\n'), '空白 token 守恒');
});

test(UNIT, '非法 options 回退默认值', function () {
  const source = 'run() {\nprint(1);\n}\n';
  assertEqual(formatWell(source, { tabSize: -2 }), 'run() {\n    print(1);\n}\n', '负数 tabSize 回退 4');
  assertEqual(formatWell(source, { tabSize: 0 }), 'run() {\nprint(1);\n}\n', 'tabSize=0 不缩进');
  assertEqual(formatWell(source, {}), 'run() {\n    print(1);\n}\n', '空 options 默认 4 空格');
});

test(UNIT, '综合样例：注释 token 守恒 + 幂等', function () {
  const source = '// head\nvar a: int = 1; // tail\n/* block */\nfunc f() {}\n';
  const formatted = formatWell(source);
  assertTokensPreserved(source, formatted, 'crafted token 守恒');
  assertClean(formatted, 'crafted 格式');
  assertEqual(formatWell(formatted), formatted, 'crafted 幂等');
});

// ---------------------------------------------------------------------------
// 汇总输出
// ---------------------------------------------------------------------------

console.log('[format.test] 真实 .we 文件扫描目录: ' + compilerRoot);
console.log('[format.test] 扫描真实 .we 文件: ' + realFiles.length + ' 个（跳过生成/依赖目录: ' + Array.from(SKIP_DIRS).sort().join(', ') + '）');
console.log('[format.test] 真实文件断言: ' + stats.real.pass + ' 通过 / ' + stats.real.fail + ' 失败');
console.log('[format.test] 手写用例: ' + stats.unit.pass + ' 通过 / ' + stats.unit.fail + ' 失败');

const totalPass = stats.real.pass + stats.unit.pass;
const totalFail = stats.real.fail + stats.unit.fail;

if (totalFail > 0) {
  console.error('');
  for (const failure of failures) {
    console.error('FAIL [' + failure.group + '] ' + failure.name);
    const message = failure.error && failure.error.message ? failure.error.message : String(failure.error);
    for (const line of message.split('\n')) {
      console.error('     ' + line);
    }
  }
  console.error('');
  console.error('[format.test] 合计: ' + totalPass + ' 通过 / ' + totalFail + ' 失败');
  process.exitCode = 1;
} else {
  console.log('[format.test] 合计: ' + totalPass + ' 通过 / 0 失败');
  console.log('[format.test] PASS');
}
