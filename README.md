# MPE Eolink 数据源插件 (`mpe-plugin-eolink`)

[English Version](./README_EN.md)

> **定位**：本仓库是 MPE (Multi-Protocol Flow Executor) 的**官方数据源插件之一**。它基于标准 stdio 上的 JSON-RPC 2.0 协议运行，负责与 Eolink / Apikit OpenAPI (v3) 对接，提供项目/API 树形浏览、接口详情查询以及一键将 API 转换为 MPE 画布标准 HTTP 节点模板的能力。

---

## 0. 环境要求与运行环境说明

由于本插件采用 **Python 3** 编写，使用前请了解以下运行环境要求：

### 1. Python 版本要求
- **最低版本**：Python 3.8+（已在 Python 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13 环境下测试通过）。

### 2. 零外部依赖（无需 `pip install`）
- 本插件**完全基于 Python 标准库**开发（仅使用 `urllib.request`、`json`、`argparse`、`sys`、`typing` 等标准模块）。
- **不需要任何第三方依赖包**，无需配置虚拟环境或安装任何 requirements.txt，真正开箱即用。

### 3. 系统 PATH 配置与启动命令
- 插件的元数据文件 `plugin.json` 声明了启动入口：
  ```json
  "entry": { "command": "python3", "args": ["./eolink.py"] }
  ```
- **Linux / macOS**：请确保系统已安装 Python 3，并在全局 PATH 中可通过 `python3` 命令执行（可运行 `which python3` 确认）。
- **Windows**：请确保已安装 Python 3，且在安装时勾选了 **"Add python.exe to PATH"**；如果系统中仅有 `python.exe`，可设置别名或确保 `python3` 命令可用。

---

## 1. 架构与工作方式

```
Host (mpe / mpe-cli)                    Plugin process (eolink.py)
   │  扫描 plugins/ 目录                       │
   │  ── describe ───────────────────────────► │  返回数据源描述与配置项 schema
   │  ◄─────────── datasource spec ─────────── │
   │  ── validate_config(config) ────────────► │  校验 base_url / token / space_id 连通性
   │  ◄─────────── { valid: true } ─────────── │
   │  ── browse(config, resource, page) ─────► │  分页查询项目或 API 列表
   │  ◄─────────── item list ───────────────── │
   │  ── get_templates(config, item_ids) ────► │  拉取 API 详情并转为 HTTP 节点模板
   │  ◄─────────── node templates ──────────── │
```

- **通信通道**：标准输入/输出（stdin/stdout），每行一个 JSON-RPC 2.0 文档（LF 换行）。
- **驻留模式**：按需拉起（OnDemand），轻量无状态。
- **拖拽导入**：在 MPE 前端界面浏览 Eolink API 时，直接拖入画布即可生成具备完整 URL、Params、Headers、JSON Body 及参数示例的 HTTP 请求节点。

---

## 2. 项目结构

```
mpe-plugin-eolink/
├── eolink.py                 # 插件主逻辑（JSON-RPC 循环 + Apikit 客户端 + 节点模板转换器 + 自测套件）
├── plugin.json               # 插件清单文件（声明 entry、kind、capabilities 等）
├── .gitignore                # Git 忽略配置（忽略 Python 缓存、虚拟环境、打包产物）
├── .github/
│   └── workflows/
│       └── ci.yml            # CI 自动化自测 + 跨平台 zip 打包 + Release 发布 + Registry 同步
├── README.md                 # 中文说明文档（默认）
└── README_EN.md              # 英文说明文档
```

---

## 3. 支持的 Apikit / Eolink OpenAPI 规范

本插件对接 Apikit / Eolink OpenAPI v3 接口体系：
- **项目列表**：`GET /v3/api-management/projects?space_id={space_id}`
- **API 列表**：`GET /v3/api-management/apis?project_id={project_id}&space_id={space_id}&page={page}&size={size}`
- **API 详情**：`GET /v3/api-management/api?project_id={project_id}&space_id={space_id}&api_id={api_id}`

### 特性支持
1. **自动路由适配**：自动兼容 `http://uat.apikit.com/index.php` 或自定义前缀（如 `/index.php`）。
2. **请求类型映射**：自动识别 `api_request_type` 整数枚举（0:POST, 1:GET, 2:PUT, 3:DELETE, 4:HEAD, 5:OPTIONS, 6:PATCH）。
3. **参数与 Body 递归转换**：递归将 Eolink 的 JSON / Form 参数结构体转换为格式化的 JSON 示例请求体与 Query/Header 参数列表。

---

## 4. 数据源配置参数

在 MPE 数据源配置面板中提供以下参数：

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `base_url` | string | 是 | `http://uat.apikit.com/index.php` | Eolink / Apikit 服务基础地址 |
| `token` / `api_key` | string | 是 | - | Apikit OpenAPI 调用凭证 |
| `space_id` | string | 是 | - | 空间 ID (Space ID) |
| `project_id` | string | 否 | - | 项目 ID。若配置则直接进入该项目下的 API 列表 |

---

## 5. 本地开发与测试

### 运行内置离线自测试套件
插件内置了完整的 Mock 离线自测套件，覆盖 `describe`、`validate_config`、`browse`、`get_item`、`get_templates` 等全流程：

```bash
python3 eolink.py --self-test
```

输出示例：
```
[Self-Test] Starting Eolink datasource plugin self-test...
  ✓ describe test passed
  ✓ validate_config (empty) test passed
  ✓ browse(project) Apikit v3 test passed
  ✓ browse(api) Apikit v3 test passed
  ✓ get_item Apikit v3 test passed
  ✓ get_templates (convert_api_to_template) Apikit v3 test passed

[Self-Test] Result: PASS
```

### 标准输入输出交互测试
启动 stdio 循环：
```bash
python3 eolink.py
```
输入 JSON-RPC 请求：
```json
{"jsonrpc":"2.0","id":1,"method":"describe","params":{}}
```

---

## 6. 构建与发布

当向本仓库推送 `v*` 标签（例如 `v0.1.0`）时，GitHub Actions 会自动：
1. 运行 `eolink.py --self-test` 自测；
2. 为各平台打包分发包（`eolink-<version>-<platform>.zip` 及 `.sha256` 校验和）；
3. 发布 GitHub Release；
4. 自动触发 `multi-protocol-flow/mpe-plugin-registry` 仓库的工作流，完成官方插件市场注册表的同步与收录。
