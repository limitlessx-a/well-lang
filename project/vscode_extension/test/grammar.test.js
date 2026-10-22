'use strict';

/**
 * Well VSCode 扩展：语法高亮 / 语言配置 / 代码片段 静态校验脚本。
 *
 * 纯 node 实现（只依赖内置 fs / path，不依赖 node_modules）。
 * 运行方式：node test/grammar.test.js
 * 任一断言失败 → 退出码 1；全部通过 → 退出码 0。
 *
 * 校验内容：
 *  1. 三个 JSON 文件存在且可被 JSON.parse；
 *  2. TextMate 语法：scopeName / fileTypes / patterns / repository / 关键 scope；
 *  3. 语法中所有正则均可编译（Oniguruma 兼容语法，JS RegExp 可兜底校验转义）；
 *  4. 语法中所有 #include 都能在 repository 中解析；
 *  5. 用一个小型“行级 tokenizer”验证关键语法真的被分到正确 scope
 *     （字符串中的 // 不得当作注释、for-of/for-in、switch/case、三元、??、+=、->、
 *       泛型、内置对象/函数、声明名、成员访问、数字、注释等）；
 *  6. snippets：JSON 合法、数量、prefix 唯一、body 含占位符；
 *  7. language-configuration：comments / brackets / autoClosingPairs / surroundingPairs /
 *     wordPattern / indentationRules / onEnterRules / folding；
 *  8. package.json 中对三个文件的引用与实际文件一致（只读校验，不修改）。
 */

const fs = require('fs');
const path = require('path');

const EXT_ROOT = path.resolve(__dirname, '..');
const GRAMMAR_PATH = path.join(EXT_ROOT, 'syntaxes', 'well.tmLanguage.json');
const LANGUAGE_CONFIG_PATH = path.join(EXT_ROOT, 'language-configuration.json');
const SNIPPETS_PATH = path.join(EXT_ROOT, 'snippets', 'well.json');
const PACKAGE_PATH = path.join(EXT_ROOT, 'package.json');

let passed = 0;
const failures = [];

function check(cond, message) {
  if (cond) {
    passed += 1;
    console.log('  ok   ' + message);
  } else {
    failures.push(message);
    console.log('  FAIL ' + message);
  }
}

function section(title) {
  console.log('\n[' + title + ']');
}

function readText(file) {
  let text = fs.readFileSync(file, 'utf8');
  if (text.charCodeAt(0) === 0xfeff) {
    text = text.slice(1);
  }
  return text;
}

function readJson(file, label) {
  if (!fs.existsSync(file)) {
    check(false, label + ' 文件存在');
    return null;
  }
  try {
    return JSON.parse(readText(file));
  } catch (err) {
    check(false, label + ' 是合法 JSON（' + err.message + '）');
    return null;
  }
}

function compileCheck(re, message) {
  try {
    new RegExp(re);
    check(true, message);
    return true;
  } catch (err) {
    check(false, message + '（' + err.message + '）');
    return false;
  }
}

// ---------------------------------------------------------------------------
// 1. TextMate 语法
// ---------------------------------------------------------------------------

const REQUIRED_SCOPES = [
  // 注释 / 字符串
  'comment.line.double-slash.well',
  'comment.block.well',
  'string.quoted.double.well',
  'constant.character.escape.well',
  // 数字
  'constant.numeric.integer.well',
  'constant.numeric.float.well',
  // 关键字 / 字面量 / 运算符
  'keyword.other.well',
  'keyword.control.well',
  'keyword.operator.well',
  'constant.language.well',
  // 类型 / 内置类型 / 内置对象 / 内置函数
  'support.type.well',
  'support.class.well',
  'support.function.well',
  // 声明名 / 调用名 / 成员
  'entity.name.type.well',
  'entity.name.function.well',
  'variable.other.property.well',
  // 标点
  'punctuation.definition.block.begin.well',
  'punctuation.definition.group.begin.well',
  'punctuation.separator.well',
  'punctuation.terminator.well'
];

function walkGrammar(node, visit) {
  if (Array.isArray(node)) {
    for (const item of node) walkGrammar(item, visit);
    return;
  }
  if (node && typeof node === 'object') {
    visit(node);
    for (const value of Object.values(node)) walkGrammar(value, visit);
  }
}

function checkGrammar(grammar) {
  section('TextMate 语法 (syntaxes/well.tmLanguage.json)');

  check(typeof grammar.name === 'string' && grammar.name.length > 0, '包含 name 字段');
  check(grammar.scopeName === 'source.well', 'scopeName === "source.well"');
  check(
    Array.isArray(grammar.fileTypes) && grammar.fileTypes.indexOf('we') >= 0,
    'fileTypes 包含 "we"'
  );
  check(
    Array.isArray(grammar.patterns) && grammar.patterns.length > 0,
    '存在非空 patterns 数组'
  );
  check(
    grammar.repository && typeof grammar.repository === 'object',
    '存在 repository 对象'
  );

  const raw = JSON.stringify(grammar);
  for (const scope of REQUIRED_SCOPES) {
    check(raw.indexOf('"' + scope + '"') >= 0, '关键 scope 存在：' + scope);
  }

  // 所有正则（match / begin / end / while）必须可编译
  const regexes = [];
  walkGrammar(grammar, (node) => {
    for (const key of ['match', 'begin', 'end', 'while']) {
      if (typeof node[key] === 'string') regexes.push(node[key]);
    }
  });
  check(regexes.length >= 20, '语法中正则数量合理（' + regexes.length + ' 条）');

  let badRegex = 0;
  for (const re of regexes) {
    try {
      new RegExp(re);
    } catch (err) {
      badRegex += 1;
      console.log('       无法编译的正则：' + re + ' :: ' + err.message);
    }
  }
  check(badRegex === 0, '所有正则均可编译（不兼容 \\A / \\G 等写法）');

  // #include 必须能解析到 repository
  const repoKeys = Object.keys(grammar.repository || {});
  const includeKeys = [];
  walkGrammar(grammar, (node) => {
    if (typeof node.include === 'string' && node.include.charAt(0) === '#') {
      includeKeys.push(node.include.slice(1));
    }
  });
  const missing = includeKeys.filter((key) => repoKeys.indexOf(key) < 0);
  check(
    missing.length === 0,
    '所有 #include 均能解析到 repository' + (missing.length ? '（缺失：' + missing.join(', ') + '）' : '')
  );

  const topIncludes = (grammar.patterns || [])
    .filter((p) => typeof p.include === 'string')
    .map((p) => p.include.replace(/^#/, ''));
  check(
    topIncludes.indexOf('comments') >= 0 && topIncludes.indexOf('strings') >= 0,
    '顶层 patterns 优先包含 comments 与 strings'
  );

  // 注释 / 字符串 / 关键运算符的原始写法检查
  check(raw.indexOf('//.*$') >= 0, '行注释正则以 // 开头');
  check(raw.indexOf('/\\\\*') >= 0, '块注释以 /* 开始');

  // 块注释必须是 begin/end 形式（而不是单行 match），才能支持跨行 /* ... */
  const blockCommentRules = [];
  walkGrammar(grammar, (node) => {
    if (node.begin === '/\\*' && node.end === '\\*/') blockCommentRules.push(node);
  });
  check(
    blockCommentRules.length === 1 && blockCommentRules[0].name === 'comment.block.well',
    '块注释使用 begin/end 规则（支持跨行 /* ... */）'
  );

  const operatorSources = regexes.join('\n');
  const expectedOperators = [
    '==|!=|<=|>=',
    '->',
    '\\?\\?',
    '&&|\\|\\|',
    '\\+=|-=',
    '=(?!=)',
    '\\?',
    '[+\\-*/%]',
    '[&|^~]',
    '!',
    '[<>]'
  ];
  for (const source of expectedOperators) {
    check(operatorSources.indexOf(source) >= 0, '运算符已被语法覆盖：' + source);
  }
}

// ---------------------------------------------------------------------------
// 2. 小型行级 tokenizer（模拟 TextMate 的 begin/match 优先级，用于行为验证）
// ---------------------------------------------------------------------------

function resolveRules(grammar) {
  const repo = grammar.repository || {};
  const rules = [];

  function resolve(patterns) {
    for (const pattern of patterns) {
      if (pattern && typeof pattern.include === 'string') {
        const key = pattern.include.replace(/^#/, '');
        const entry = repo[key];
        if (!entry) continue;
        if (entry.match || entry.begin) {
          resolve([entry]);
        } else {
          resolve(entry.patterns || []);
        }
        continue;
      }
      if (pattern && (pattern.match || pattern.begin)) {
        rules.push(pattern);
      }
    }
  }

  resolve(grammar.patterns || []);
  return rules;
}

function tryMatch(rule, line, index) {
  const re = new RegExp(rule.match, 'y');
  re.lastIndex = index;
  const m = re.exec(line);
  if (!m || m[0].length === 0) return null;
  return m;
}

function findEnd(rule, line, from) {
  const innerRules = (rule.patterns || []).filter((p) => p && p.match);
  let index = from;
  while (index < line.length) {
    let consumed = false;
    for (const inner of innerRules) {
      const m = tryMatch(inner, line, index);
      if (m) {
        index += m[0].length;
        consumed = true;
        break;
      }
    }
    if (consumed) continue;
    const endRe = new RegExp(rule.end, 'y');
    endRe.lastIndex = index;
    const endMatch = endRe.exec(line);
    if (endMatch && endMatch[0].length > 0) {
      return index + endMatch[0].length;
    }
    index += 1;
  }
  return line.length;
}

/**
 * 把一行源码切成 [{ scope, text, start, end }]，scope 为 null 表示未着色。
 * 只实现本语法实际用到的能力（match + begin/end + captures），足够验证高亮行为。
 */
function scanLine(line, rules) {
  const spans = [];
  let index = 0;

  while (index < line.length) {
    // 与 TextMate 一致：按语法中声明的顺序逐条尝试，match 与 begin 同场竞争，先匹配者胜。
    let rule = null;
    let match = null;
    let beginMatch = null;

    for (const candidate of rules) {
      if (candidate.match) {
        const m = tryMatch(candidate, line, index);
        if (m) {
          rule = candidate;
          match = m;
          break;
        }
      } else if (candidate.begin) {
        const beginRe = new RegExp(candidate.begin, 'y');
        beginRe.lastIndex = index;
        const bm = beginRe.exec(line);
        if (bm && bm[0].length > 0) {
          rule = candidate;
          beginMatch = bm;
          break;
        }
      }
    }

    if (!rule) {
      spans.push({ scope: null, text: line.charAt(index), start: index, end: index + 1 });
      index += 1;
      continue;
    }

    if (beginMatch) {
      const end = findEnd(rule, line, index + beginMatch[0].length);
      spans.push({
        scope: rule.name || null,
        text: line.slice(index, end),
        start: index,
        end
      });
      index = end;
      continue;
    }

    const full = match[0];
    const captures = rule.captures || {};
    const captureKeys = Object.keys(captures).map(Number).sort((a, b) => a - b);
    let cursor = index;

    if (captureKeys.length > 0) {
      for (const key of captureKeys) {
        const text = match[key];
        if (text === undefined || text === null) continue;
        const found = full.indexOf(text, cursor - index);
        if (found < 0) continue;
        const start = index + found;
        if (start > cursor) {
          spans.push({ scope: null, text: line.slice(cursor, start), start: cursor, end: start });
        }
        spans.push({
          scope: captures[key].name || null,
          text,
          start,
          end: start + text.length
        });
        cursor = start + text.length;
      }
    } else if (rule.name) {
      spans.push({ scope: rule.name, text: full, start: index, end: index + full.length });
    }

    if (cursor < index + full.length) {
      spans.push({
        scope: null,
        text: line.slice(cursor, index + full.length),
        start: cursor,
        end: index + full.length
      });
    }
    index += full.length;
  }

  return spans;
}

function scoped(spans) {
  return spans.filter((span) => span.scope);
}

function hasSpan(spans, scope, text) {
  return spans.some((span) => span.scope === scope && span.text === text);
}

function checkBehavior(grammar) {
  section('高亮行为（行级 tokenizer 冒烟测试）');
  const rules = resolveRules(grammar);
  check(rules.length > 0, '规则展开成功（' + rules.length + ' 条可执行规则）');

  // --- 字符串中的 // 不得当注释 ---
  const stringCase = scanLine('var url: String = "http://x"; // note', rules);
  const stringSpan = scoped(stringCase).find((s) => s.scope === 'string.quoted.double.well');
  const lineComments = scoped(stringCase).filter(
    (s) => s.scope === 'comment.line.double-slash.well'
  );
  check(
    stringSpan !== undefined && stringSpan.text === '"http://x"',
    '字符串 "http://x" 整体着色为 string.quoted.double.well'
  );
  check(
    lineComments.length === 1 && lineComments[0].text === '// note',
    '只有行尾的 // note 被识别为行注释（字符串内的 // 不算注释）'
  );
  check(
    stringSpan !== undefined &&
      lineComments.length === 1 &&
      lineComments[0].start >= stringSpan.end,
    '注释起点位于字符串结束之后'
  );

  // --- 字符串转义 ---
  const escapeCase = scanLine('var s: String = "a\\"b"; // c', rules);
  const escapeString = scoped(escapeCase).find((s) => s.scope === 'string.quoted.double.well');
  check(
    escapeString !== undefined && escapeString.text === '"a\\"b"',
    '字符串内的转义引号不会提前结束字符串'
  );

  // --- 块注释 ---
  const blockCase = scanLine('/* real */ var x: int = 1;', rules);
  check(
    hasSpan(scoped(blockCase), 'comment.block.well', '/* real */'),
    '/* ... */ 着色为 comment.block.well'
  );
  const blocked = scanLine('var s: String = "/* not comment */";', rules);
  check(
    scoped(blocked).filter((s) => s.scope === 'comment.block.well').length === 0,
    '字符串中的 /* ... */ 不被当作块注释'
  );

  // --- for (v of arr) / for (k in map) ---
  const forOf = scoped(scanLine('for (var v of arr) {', rules));
  check(hasSpan(forOf, 'keyword.control.well', 'for'), 'for-of：for 为 keyword.control.well');
  check(hasSpan(forOf, 'keyword.control.well', 'of'), 'for-of：of 为 keyword.control.well');
  check(hasSpan(forOf, 'keyword.other.well', 'var'), 'for-of：var 为 keyword.other.well');
  check(
    hasSpan(forOf, 'variable.other.readwrite.well', 'v'),
    'for-of：迭代变量 v 为 variable.other.readwrite.well'
  );

  const forIn = scoped(scanLine('for (k in map) {', rules));
  check(hasSpan(forIn, 'keyword.control.well', 'in'), 'for-in：in 为 keyword.control.well');
  check(
    hasSpan(forIn, 'variable.other.readwrite.well', 'k'),
    'for-in：迭代变量 k 为 variable.other.readwrite.well'
  );

  const classicFor = scoped(scanLine('for (var i: int = 0; i < 3; i = i + 1) {', rules));
  check(
    hasSpan(classicFor, 'keyword.control.well', 'for') &&
      hasSpan(classicFor, 'support.type.well', 'int'),
    'C 风格 for 循环仍正常着色'
  );

  // --- switch / case / default ---
  const switchCase = scoped(
    scanLine('switch (x) { case 1: break; default: break; }', rules)
  );
  check(hasSpan(switchCase, 'keyword.control.well', 'switch'), 'switch 为 keyword.control.well');
  check(hasSpan(switchCase, 'keyword.control.well', 'case'), 'case 为 keyword.control.well');
  check(hasSpan(switchCase, 'keyword.control.well', 'default'), 'default 为 keyword.control.well');
  check(hasSpan(switchCase, 'keyword.control.well', 'break'), 'break 为 keyword.control.well');
  check(
    hasSpan(switchCase, 'constant.numeric.integer.well', '1'),
    'case 1 中数字为 constant.numeric.integer.well'
  );

  // --- 泛型 ---
  const generic = scoped(scanLine('var a: Array<String> = [1, 2];', rules));
  check(hasSpan(generic, 'support.type.well', 'Array'), '泛型容器 Array 为 support.type.well');
  check(hasSpan(generic, 'support.type.well', 'String'), '泛型参数 String 为 support.type.well');

  // --- 运算符 ---
  const operators = scoped(
    scanLine('x = a ? b : c; y = a ?? b; z += 1; q -= 2; p -> r; s == t; u && v;', rules)
  );
  for (const op of ['?', '??', '+=', '-=', '->', '==', '&&']) {
    check(hasSpan(operators, 'keyword.operator.well', op), '运算符 ' + op + ' 为 keyword.operator.well');
  }

  // --- 内置对象 / 内置函数 / 成员访问 ---
  const builtins = scoped(scanLine('print(Model.get(e.n));', rules));
  check(hasSpan(builtins, 'support.function.well', 'print'), 'print 为 support.function.well');
  check(hasSpan(builtins, 'support.class.well', 'Model'), 'Model 为 support.class.well');
  check(
    hasSpan(builtins, 'entity.name.function.well', 'get'),
    'Model.get 的成员调用 get 为 entity.name.function.well'
  );
  check(
    hasSpan(builtins, 'variable.other.property.well', 'n'),
    'e.n 的成员访问 n 为 variable.other.property.well'
  );

  const builtinObjects = scoped(
    scanLine('Cache Json IdUtil Http Ws Mysql File Host Timer;', rules)
  );
  for (const name of ['Cache', 'Json', 'IdUtil', 'Http', 'Ws', 'Mysql', 'File', 'Host', 'Timer']) {
    check(hasSpan(builtinObjects, 'support.class.well', name), '内置对象 ' + name + ' 为 support.class.well');
  }
  const builtinFns = scoped(scanLine('on once off emit start stop;', rules));
  for (const name of ['on', 'once', 'off', 'emit', 'start', 'stop']) {
    check(hasSpan(builtinFns, 'support.function.well', name), '内置函数 ' + name + ' 为 support.function.well');
  }

  // --- 声明名 ---
  const structDecl = scoped(scanLine('struct Foo extends Vo {', rules));
  check(
    hasSpan(structDecl, 'entity.name.type.well', 'Foo'),
    'struct 后的声明名为 entity.name.type.well'
  );
  check(hasSpan(structDecl, 'support.class.well', 'Vo'), '根类型 Vo 为 support.class.well');
  check(
    hasSpan(structDecl, 'keyword.other.well', 'extends'),
    'extends 为 keyword.other.well'
  );

  const funcDecl = scoped(scanLine('func bar(a: int): int { return 0x1F; }', rules));
  check(
    hasSpan(funcDecl, 'entity.name.function.well', 'bar'),
    'func 后的声明名为 entity.name.function.well'
  );
  check(
    hasSpan(funcDecl, 'constant.numeric.integer.well', '0x1F'),
    '十六进制 0x1F 为 constant.numeric.integer.well'
  );

  const varDecl = scoped(scanLine('var count: long = 42;', rules));
  check(
    hasSpan(varDecl, 'variable.other.readwrite.well', 'count'),
    'var 后的变量名为 variable.other.readwrite.well'
  );
  check(hasSpan(varDecl, 'support.type.well', 'long'), 'long 为 support.type.well');

  // --- 字面量 / 数字 ---
  const literals = scoped(
    scanLine('var ok: bool = true; var n: double = 1.5e3; var z: String = null; emit(e, ALL);', rules)
  );
  check(hasSpan(literals, 'constant.language.well', 'true'), 'true 为 constant.language.well');
  check(hasSpan(literals, 'constant.language.well', 'null'), 'null 为 constant.language.well');
  check(hasSpan(literals, 'constant.language.well', 'ALL'), 'ALL 为 constant.language.well');
  check(
    hasSpan(literals, 'constant.numeric.float.well', '1.5e3'),
    '科学计数 1.5e3 为 constant.numeric.float.well'
  );

  // --- event 字段初始化 / 对象字面量 ---
  const objectLiteral = scoped(scanLine('emit(new PingEvent(7) { n: 7 });', rules));
  check(
    hasSpan(objectLiteral, 'entity.name.type.well', 'PingEvent'),
    'new 后的类型名 PingEvent 为 entity.name.type.well'
  );
  check(
    hasSpan(objectLiteral, 'punctuation.separator.colon.well', ':'),
    '对象字面量字段的 : 为 punctuation.separator.colon.well'
  );
}

// ---------------------------------------------------------------------------
// 3. snippets
// ---------------------------------------------------------------------------

function checkSnippets(snippets) {
  section('代码片段 (snippets/well.json)');

  const entries = Object.entries(snippets);
  check(entries.length >= 16, '片段数量 >= 16（实际 ' + entries.length + ' 条）');

  const prefixes = new Set();
  const duplicates = [];

  for (const [name, snippet] of entries) {
    check(snippet && typeof snippet === 'object', '片段 "' + name + '" 是对象');
    if (!snippet || typeof snippet !== 'object') continue;

    check(
      typeof snippet.prefix === 'string' && snippet.prefix.trim().length > 0,
      '片段 "' + name + '" 有非空 prefix'
    );
    check(
      typeof snippet.prefix === 'string' && /^[A-Za-z][A-Za-z0-9_-]*$/.test(snippet.prefix),
      '片段 "' + name + '" 的 prefix 合法（' + snippet.prefix + '）'
    );
    if (prefixes.has(snippet.prefix)) duplicates.push(snippet.prefix);
    prefixes.add(snippet.prefix);

    const body = Array.isArray(snippet.body)
      ? snippet.body
      : typeof snippet.body === 'string'
        ? [snippet.body]
        : null;
    check(Array.isArray(body) && body.length > 0, '片段 "' + name + '" 的 body 非空');
    check(
      Array.isArray(body) && body.every((line) => typeof line === 'string'),
      '片段 "' + name + '" 的 body 全部是字符串'
    );
    check(
      Array.isArray(body) && /\$(?:\{\d+[:}]|\d)/.test(body.join('\n')),
      '片段 "' + name + '" 含 $1 / ${2:...} 占位符'
    );
    check(
      typeof snippet.description === 'string' && snippet.description.length > 0,
      '片段 "' + name + '" 有 description'
    );
  }

  check(duplicates.length === 0, '所有 prefix 唯一' + (duplicates.length ? '（重复：' + duplicates.join(', ') + '）' : ''));
  check(prefixes.size === entries.length, '前缀数量与片段数量一致（' + prefixes.size + '）');

  const requiredPrefixes = [
    'module',
    'import',
    'struct',
    'event',
    'handler',
    'controller',
    'static',
    'namespace',
    'func',
    'enum',
    'if',
    'for',
    'switch',
    'on',
    'emit',
    'timeronce'
  ];
  for (const prefix of requiredPrefixes) {
    check(prefixes.has(prefix), '覆盖必需 prefix：' + prefix);
  }
}

// ---------------------------------------------------------------------------
// 4. language-configuration
// ---------------------------------------------------------------------------

function checkLanguageConfiguration(config) {
  section('语言配置 (language-configuration.json)');

  const VALID_INDENTS = ['none', 'indent', 'indentOutdent', 'outdent'];

  check(!!config.comments, '包含 comments');
  check(
    config.comments && config.comments.lineComment === '//',
    'comments.lineComment === "//"'
  );
  check(
    config.comments &&
      Array.isArray(config.comments.blockComment) &&
      config.comments.blockComment.length === 2 &&
      config.comments.blockComment[0] === '/*' &&
      config.comments.blockComment[1] === '*/',
    'comments.blockComment === ["/*", "*/"]'
  );

  check(Array.isArray(config.brackets) && config.brackets.length > 0, '包含 brackets');
  for (const pair of [['{', '}'], ['[', ']'], ['(', ')']]) {
    check(
      Array.isArray(config.brackets) &&
        config.brackets.some((b) => Array.isArray(b) && b[0] === pair[0] && b[1] === pair[1]),
      'brackets 含 ' + pair[0] + ' ' + pair[1]
    );
  }

  check(
    Array.isArray(config.autoClosingPairs) && config.autoClosingPairs.length > 0,
    '包含 autoClosingPairs'
  );
  const autoOpen = (open) =>
    (config.autoClosingPairs || []).filter((p) => p && p.open === open);
  check(autoOpen('{').length > 0, 'autoClosingPairs 含 { }');
  check(autoOpen('(').length > 0, 'autoClosingPairs 含 ( )');
  const quotePair = autoOpen('"')[0];
  check(!!quotePair, 'autoClosingPairs 含双引号');
  check(
    quotePair &&
      Array.isArray(quotePair.notIn) &&
      quotePair.notIn.indexOf('string') >= 0 &&
      quotePair.notIn.indexOf('comment') >= 0,
    '双引号 autoClosingPairs 的 notIn 含 string / comment'
  );

  check(
    Array.isArray(config.surroundingPairs) &&
      config.surroundingPairs.some((p) => Array.isArray(p) && p[0] === '{' && p[1] === '}'),
    '包含 surroundingPairs（含 { }）'
  );

  check(typeof config.wordPattern === 'string' && config.wordPattern.length > 0, '包含 wordPattern');
  if (typeof config.wordPattern === 'string') {
    compileCheck(config.wordPattern, 'wordPattern 正则可编译');
  }

  check(!!config.indentationRules, '包含 indentationRules');
  check(
    config.indentationRules &&
      typeof config.indentationRules.increaseIndentPattern === 'string',
    'indentationRules.increaseIndentPattern 存在'
  );
  check(
    config.indentationRules &&
      typeof config.indentationRules.decreaseIndentPattern === 'string',
    'indentationRules.decreaseIndentPattern 存在'
  );
  if (config.indentationRules) {
    compileCheck(
      config.indentationRules.increaseIndentPattern,
      'increaseIndentPattern 正则可编译（{ 后缩进）'
    );
    compileCheck(
      config.indentationRules.decreaseIndentPattern,
      'decreaseIndentPattern 正则可编译（} 前减少缩进）'
    );
  }

  check(
    Array.isArray(config.onEnterRules) && config.onEnterRules.length > 0,
    '包含 onEnterRules'
  );
  (config.onEnterRules || []).forEach((rule, index) => {
    check(
      rule &&
        rule.action &&
        VALID_INDENTS.indexOf(rule.action.indent) >= 0,
      'onEnterRules[' + index + '] 的 action.indent 合法'
    );
    if (rule && typeof rule.beforeText === 'string') {
      compileCheck(rule.beforeText, 'onEnterRules[' + index + '].beforeText 正则可编译');
    }
  });

  check(!!config.folding && !!config.folding.markers, '包含 folding.markers');
  if (config.folding && config.folding.markers) {
    compileCheck(config.folding.markers.start, 'folding.markers.start 正则可编译');
    compileCheck(config.folding.markers.end, 'folding.markers.end 正则可编译');
  }
}

// ---------------------------------------------------------------------------
// 5. package.json 引用一致性（只读，不修改）
// ---------------------------------------------------------------------------

function checkPackage(pkg, grammar, snippetsPath, configPath) {
  section('package.json 引用一致性（只读校验）');
  if (!pkg) return;

  const contributes = pkg.contributes || {};
  const languages = contributes.languages || [];
  const wellLanguage = languages.find((lang) => lang && lang.id === 'well');
  check(!!wellLanguage, 'contributes.languages 含 id = "well"');
  check(
    !!wellLanguage &&
      Array.isArray(wellLanguage.extensions) &&
      wellLanguage.extensions.indexOf('.we') >= 0,
    '语言贡献注册了 .we 扩展名'
  );
  check(
    !!wellLanguage && wellLanguage.configuration === './language-configuration.json',
    'language.configuration 指向 ./language-configuration.json'
  );

  const grammars = contributes.grammars || [];
  const wellGrammar = grammars.find((g) => g && g.language === 'well');
  check(!!wellGrammar, 'contributes.grammars 含 language = "well"');
  check(
    !!wellGrammar && wellGrammar.scopeName === grammar.scopeName,
    'grammar.scopeName 与 tmLanguage 的 scopeName 一致'
  );
  check(
    !!wellGrammar && wellGrammar.path === './syntaxes/well.tmLanguage.json',
    'grammar.path 指向 ./syntaxes/well.tmLanguage.json'
  );
  check(
    !!wellGrammar && fs.existsSync(path.resolve(EXT_ROOT, wellGrammar.path)),
    'grammar.path 对应文件存在'
  );

  const contributions = contributes.snippets || [];
  const wellSnippets = contributions.find((s) => s && s.language === 'well');
  check(!!wellSnippets, 'contributes.snippets 含 language = "well"');
  check(
    !!wellSnippets && wellSnippets.path === './snippets/well.json',
    'snippets.path 指向 ./snippets/well.json'
  );
  check(
    !!wellSnippets && fs.existsSync(path.resolve(EXT_ROOT, wellSnippets.path)),
    'snippets.path 对应文件存在'
  );
  check(
    fs.existsSync(path.resolve(EXT_ROOT, './language-configuration.json')) &&
      path.resolve(EXT_ROOT, './language-configuration.json') === configPath,
    'language-configuration.json 路径一致'
  );
  check(
    path.resolve(EXT_ROOT, './snippets/well.json') === snippetsPath,
    'snippets 实际路径一致'
  );
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

function main() {
  console.log('Well 扩展静态校验（grammar / language-configuration / snippets）');
  console.log('扩展目录：' + EXT_ROOT);

  section('文件存在性');
  check(fs.existsSync(GRAMMAR_PATH), '存在 syntaxes/well.tmLanguage.json');
  check(fs.existsSync(LANGUAGE_CONFIG_PATH), '存在 language-configuration.json');
  check(fs.existsSync(SNIPPETS_PATH), '存在 snippets/well.json');

  const grammar = readJson(GRAMMAR_PATH, 'syntaxes/well.tmLanguage.json');
  const languageConfig = readJson(LANGUAGE_CONFIG_PATH, 'language-configuration.json');
  const snippets = readJson(SNIPPETS_PATH, 'snippets/well.json');
  const pkg = readJson(PACKAGE_PATH, 'package.json');

  if (grammar) checkGrammar(grammar);
  if (grammar) checkBehavior(grammar);
  if (snippets) checkSnippets(snippets);
  if (languageConfig) checkLanguageConfiguration(languageConfig);
  if (pkg && grammar) checkPackage(pkg, grammar, SNIPPETS_PATH, LANGUAGE_CONFIG_PATH);

  console.log('\n----------------------------------------');
  if (failures.length > 0) {
    console.log('FAILED：' + failures.length + ' 项断言失败，' + passed + ' 项通过');
    for (const failure of failures) {
      console.log('  - ' + failure);
    }
    process.exit(1);
  }
  console.log('PASSED：' + passed + ' 项断言全部通过（退出码 0）');
  process.exit(0);
}

try {
  main();
} catch (err) {
  console.error('\n校验脚本异常：' + (err && err.stack ? err.stack : err));
  process.exit(1);
}
