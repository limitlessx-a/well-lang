# Well Language（VSCode 插件）

Well（`.we`）语言的 VS Code 插件，提供：

- **语法高亮**：关键字、类型、内置对象/函数、声明名、成员访问、字符串、注释、数字、运算符。
- **错误检查**：保存/输入时调用 `wellc check --json`，把词法、语法、语义错误以及未解析的 `import` 显示到“问题”面板，支持跨文件（被导入文件）诊断。
- **格式化**：文档格式化（`formatWell`），支持 `editor.formatOnSave`。
- **自动导入 / 整理包**：
  - 补全时自动建议工作区其它 `.we` 文件中的顶层声明，选中后自动插入 `import "..."`；
  - 代码操作（灯泡）与命令「Well: 自动导入光标处符号」；
  - 「整理导入」：排序、去重、移除未使用的导入（可用作 `source.organizeImports`，配合 `editor.codeActionsOnSave`）。
- 常用代码片段（struct / event / handler / controller / static / namespace / func / enum / 事件订阅等）。

## 安装

### 方式一：安装打包好的 VSIX（推荐）

```powershell
cd E:\Work\WellLang\project\vscode_extension
npm install            # 首次需要
npm run compile        # 编译 TypeScript -> out/
npm run package        # 生成 well-language-0.1.0.vsix
code --install-extension well-language-0.1.0.vsix
```

然后重载 VS Code 窗口（`Developer: Reload Window`）。

### 方式二：源码开发模式

用 VS Code 打开 `E:\Work\WellLang\project\vscode_extension`，按 `F5` 启动扩展开发宿主窗口；打开任意 `.we` 文件即可。修改 `src/*.ts` 后使用 `npm run watch` 自动编译。

## 依赖

1. **VS Code ≥ 1.81**
2. **Python 3**（运行 `wellc.py`，用于错误检查）
3. **Well 编译器**：插件会在工作区中自动查找 `compiler/wellc.py`、`project/compiler/wellc.py`、`wellc.py`；也可用 `well.compilerPath` 显式指定。

> 格式化、自动导入、整理导入、语法高亮全部在插件内实现，不依赖 Python；只有“错误检查”需要 Python + wellc。
>
> 若工作区打开的是 `E:\Work\WellLang` 仓库根目录，插件会自动找到 `project/compiler/wellc.py`，无需配置。

## 功能说明

### 错误检查

- 编辑时防抖（默认 400ms）检查，保存时立即检查。
- 使用命令 `Well: 检查当前文件` 手动触发。
- 诊断来源为 `wellc`，可在“问题”面板按文件查看；被导入文件中的语法错误也会显示。
- 检查使用 `python <wellc.py> check --json <file>`，不生成可执行文件、不写构建产物。

### 格式化

- 命令 `Well: 格式化当前文件` 或 `Shift+Alt+F`。
- 快捷键/保存时格式化由 VS Code 设置控制：

  ```json
  "[well]": {
    "editor.formatOnSave": true,
    "editor.defaultFormatter": "well-lang.well-language"
  }
  ```

- 缩进跟随编辑器设置（`editor.tabSize` / `editor.insertSpaces`），默认 4 空格；统一 LF 结尾、行尾无空白、保留注释与字符串原文。

### 自动导入 / 整理包

- 输入其它文件中的类型/控制器/函数名前几个字符，补全列表会显示该符号及其来源文件；选中即自动插入 `import "相对路径";`。
- 光标放在未导入的符号上，点击灯泡 → `导入 'X'（…）`；或执行命令 `Well: 自动导入光标处符号`。
- 整理导入：
  - 命令 `Well: 整理包/导入`；
  - 代码操作 `source.organizeImports`；
  - 保存时自动整理：

    ```json
    "[well]": {
      "editor.codeActionsOnSave": {
        "source.organizeImports": "explicit"
      }
    }
    ```

- 整理规则：按路径排序、去重、移除未使用的导入。仅当被导入文件能被解析时才判断“未使用”，避免误删。

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `well.enable` | `true` | 总开关（不影响语法高亮） |
| `well.pythonPath` | `"python"` | 运行 wellc 的 Python 解释器 |
| `well.compilerPath` | `""` | `wellc.py` 路径；留空自动查找 |
| `well.diagnostics.enable` | `true` | 启用错误检查 |
| `well.diagnostics.delayMs` | `400` | 编辑后延迟检查毫秒数 |
| `well.format.enable` | `true` | 启用格式化 |
| `well.imports.sort` | `"path"` | 整理导入时的排序方式（`path` / `none`） |
| `well.imports.removeUnused` | `true` | 整理导入时移除未使用项 |
| `well.index.exclude` | 见默认值 | 自动导入索引排除的 glob 列表 |

示例（工作区 `.vscode/settings.json`）：

```json
{
  "well.pythonPath": "python",
  "well.compilerPath": "E:/Work/WellLang/project/compiler/wellc.py",
  "well.diagnostics.delayMs": 300,
  "[well]": {
    "editor.formatOnSave": true,
    "editor.defaultFormatter": "well-lang.well-language",
    "editor.codeActionsOnSave": { "source.organizeImports": "explicit" }
  }
}
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `Well: 检查当前文件` | 立即运行错误检查 |
| `Well: 格式化当前文件` | 格式化整个文档 |
| `Well: 整理包/导入` | 排序 / 去重 / 移除未使用导入 |
| `Well: 自动导入光标处符号` | 为光标处符号插入 import |
| `Well: 显示编译器输出` | 查看 wellc 调用日志 |

## 开发与测试

```powershell
cd E:\Work\WellLang\project\vscode_extension
npm install
npm run compile        # tsc -p .
npm test               # 编译 + 4 组测试（格式化 / 导入解析 / 语法 JSON / 激活冒烟）
npm run package        # 生成 vsix（需要 @vscode/vsce）
```

- `npm test` 会先 `tsc -p .`，再编译 3 个纯 node 模块到 `build/ts/`，最后跑 `test/`：
  - `format.test.js`：扫描 `project/compiler` 下 123 个真实 `.we`，断言 token 守恒 + 幂等 + 无行尾空白，另有 22 个手写边界用例；
  - `imports.test.js`：`wellParse`/`imports` 单测（import 解析、整理/排序/去重/移除未使用、CRLF/BOM、与 `driver.py::_resolve_import` 对拍）；
  - `grammar.test.js`：TextMate/language-configuration/snippets 的 JSON 结构与关键 scope 校验；
  - `activation.test.js`：不需要 Electron，用 mock 的 `vscode` 模块加载 `out/`，验证激活注册、格式化 Provider、真实 `wellc check` 诊断链路、整理导入与自动导入命令。
- 仅跑激活冒烟：`npm run test:activation`。
- `npm run package` 产物为 `well-language-0.1.0.vsix`；`code --install-extension well-language-0.1.0.vsix` 安装后重载窗口即可。

目录结构：

```
vscode_extension/
  package.json              # 扩展清单（语言/语法/命令/配置注册）
  tsconfig.json
  language-configuration.json
  syntaxes/well.tmLanguage.json   # TextMate 语法高亮
  snippets/well.json
  src/
    extension.ts            # 激活、命令、代码操作、格式化注册
    compiler.ts             # 定位 python/wellc，调用 check --json
    diagnostics.ts          # 诊断生命周期（防抖/取消/跨文件）
    formatter.ts            # Well 格式化器（纯函数，可单测）
    wellParse.ts            # 顶层声明/import/module 扫描，import 解析
    imports.ts              # 整理导入 / addImport（纯函数）
    workspaceIndex.ts       # 工作区符号索引（自动导入）
    completion.ts           # 自动导入补全
    util.ts                 # 共享小工具
  test/                     # 纯 node 测试（含 vscode mock 激活测试）
```

## 已知限制

- 没有独立的 LSP 进程：诊断通过 `wellc check` 子进程完成，编辑大文件时有一定延迟（可调 `well.diagnostics.delayMs`）。
- `wellc` 的语义错误目前只有文件级消息，插件对这类错误做“按标识符 best-effort 定位”，行列可能落在相关声明/首次出现处，而非精确出错表达式。
- 格式化不自动折行；对同一行极长的表达式保持单行。
- 自动导入索引只覆盖当前工作区内的 `*.we`；`well.index.exclude` 可排除大目录。
- 尚未提供转到定义、查找引用、重命名、悬浮提示（可作为后续迭代）。
- 插件要求打开的工作区包含 Well 源码（依赖 `python` 与 `wellc.py` 才能做错误检查）。

## 故障排查

1. **没有报错**：确认状态栏/输出面板（`Well: 显示编译器输出`）中 `wellc.py` 已被找到；必要时设置 `well.compilerPath`。
2. **提示无法启动 Python**：检查 `well.pythonPath`（Windows 可用 `python`、`py -3` 的绝对路径）。
3. **`wellc check` 输出无法解析**：说明编译器版本过旧，需使用包含 `check` 子命令的版本（本仓库 `wellc.py check --json`）。
4. **格式化/整理导入没有反应**：检查 `well.enable`、`well.format.enable`，并确认文件语言模式为 `Well`。
