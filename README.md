# MPE Eolink 数据源插件 (`mpe-plugin-eolink`)

[English Version](./README_EN.md)

> **定位**：本仓库是 MPE (Multi-Protocol Flow Executor) 的**官方数据源插件之一**。它基于标准 stdio 上的 JSON-RPC 2.0 协议运行，负责与 Eolink / Apikit OpenAPI (v3) 对接，提供项目/API 树形浏览、接口详情查询以及一键将 API 转换为 MPE 画布标准 HTTP 节点模板的能力。

---

## 0. 运行环境与安装说明

### 1. 最终用户（通过 MPE 插件市场安装）
- **零环境依赖**：官方分发包（Windows / Linux / macOS）已通过 CI 自动打包为独立的免环境可执行二进制文件（Windows 上为 `eolink.exe`，Linux/macOS 上为 `eolink`）。
- **开箱即用**：用户的操作系统**无需预先安装 Python 或配置任何环境变量**，由 MPE 宿主管理生命周期并直接拉起运行。

### 2. 开发者（本地从源码开发与测试）
- **Python 版本要求**：Python 3.8+（已在 Python 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13 环境下测试通过）。
- **零外部依赖（无需 `pip install`）**：本插件完全基于 Python 标准库开发（仅使用 `urllib.request`、`json`、`argparse`、`sys`、`typing` 等标准模块），无需安装任何第三方 pip 包。
- **本地构建独立二进制（可选）**：如需本地编译二进制产物，只需安装 `pyinstaller`：
  ```bash
  pip install pyinstaller
  pyinstaller --onefile --name eolink eolink.py
  ```

---

## 1. 架构与工作方式

```
Host (mpe / mpe-cli)                    Plugin process (eolink / eolink.exe)
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
├── plugin.json               # 插件清单文件（声明 entry 为 ./eolink、kind、capabilities 等）
├── .gitignore                # Git 忽略配置（忽略 Python 缓存、虚拟环境、打包产物）
├── .github/
│   └── workflows/
│       └── ci.yml            # CI 自动化自测 + PyInstaller 跨平台二进制打包 + Release 发布 + Registry 同步
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
1. **自动路由适配**：自动兼容 Apikit 标准路由前缀（如 `/index.php`），也支持任意自定义服务地址。
2. **请求类型映射**：自动识别 `api_request_type` 整数枚举（0:POST, 1:GET, 2:PUT, 3:DELETE, 4:HEAD, 5:OPTIONS, 6:PATCH）。
3. **参数与 Body 递归转换**：递归将 Eolink 的 JSON / Form 参数结构体转换为格式化的 JSON 示例请求体与 Query/Header 参数列表。

---

## 4. 数据源配置参数

在 MPE 数据源配置面板中提供以下参数：

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `base_url` | string | 是 | 无（必须填写） | Eolink / Apikit 服务基础地址，插件不内置任何第三方默认地址 |
| `api_key` | string | 是 | - | Apikit OpenAPI 调用凭证（`Eo-Secret-Key`） |
| `space_id` | string | 否 | - | 空间 ID (Space ID) |

---

## 5. 本地开发与测试

### 运行内置离线自测试套件
插件内置了完整的 Mock 离线自测套件，覆盖 `describe`、`validate_config`、`browse`、`get_item`、`get_templates` 等全流程：

```bash
# 从源码运行
python3 eolink.py --self-test

# 或运行编译后的二进制
./dist/eolink --self-test
```

输出示例：
```
[Self-Test] Starting Eolink datasource plugin self-test...
  [PASS] describe test passed
  [PASS] validate_config (empty) test passed
  [PASS] browse(project) Apikit v3 test passed
  [PASS] browse(api) Apikit v3 test passed
  [PASS] get_item Apikit v3 test passed
  [PASS] get_templates (convert_api_to_template) Apikit v3 test passed

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

当向本仓库推送 `v*` 标签（例如 `v0.1.1`）时，GitHub Actions 会自动：
1. 运行 `eolink.py --self-test` 自动化离线测试；
2. 在 Ubuntu、Windows、macOS 矩阵 Runner 上使用 PyInstaller 编译为原生二进制文件；
3. 打包各平台独立分发包（`eolink-<version>-<platform>.zip` 及 `.sha256` 校验和）；
4. 发布 GitHub Release；
5. 自动触发 `multi-protocol-flow/mpe-plugin-registry` 仓库工作流，完成官方插件市场注册表的同步与更新。

## 许可证

MIT，详见 [LICENSE](./LICENSE)。
