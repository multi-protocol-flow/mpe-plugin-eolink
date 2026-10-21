# MPE Eolink Datasource Plugin (`mpe-plugin-eolink`)

[中文版本](./README.md)

> **Overview**: This repository is one of the **official datasource plugins** for MPE (Multi-Protocol Flow Executor). Running as a sidecar process communicating over standard I/O (stdio) via JSON-RPC 2.0, it integrates with Eolink / Apikit OpenAPI (v3) to provide project and API tree browsing, endpoint detail queries, and one-click drag-and-drop conversion of API definitions into standard HTTP node templates on the MPE canvas.

---

## 0. Runtime Environment & Prerequisites

### 1. End Users (Installed via MPE Plugin Marketplace)
- **Zero Runtime Dependencies**: The official release artifacts (Windows, Linux, macOS) are automatically compiled into standalone native executables (`eolink.exe` on Windows, `eolink` on Linux/macOS) via PyInstaller in CI.
- **Out of the Box**: Users **do not need to have Python installed or configured in system PATH**. The host process manages the lifecycle and executes the binary directly.

### 2. Developers (Source Development & Debugging)
- **Python Version**: Python 3.8+ (tested on Python 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13).
- **Zero External Dependencies (No `pip install` required)**: Developed purely with Python standard libraries (`urllib.request`, `json`, `argparse`, `sys`, `typing`, etc.).
- **Building Local Standalone Binaries (Optional)**:
  ```bash
  pip install pyinstaller
  pyinstaller --onefile --name eolink eolink.py
  ```

---

## 1. Architecture & Workflow

```
Host (mpe / mpe-cli)                    Plugin process (eolink / eolink.exe)
   │  scans plugins/ directory                 │
   │  ── describe ───────────────────────────► │  Returns datasource spec & config schema
   │  ◄─────────── datasource spec ─────────── │
   │  ── validate_config(config) ────────────► │  Validates base_url / token / space_id
   │  ◄─────────── { valid: true } ─────────── │
   │  ── browse(config, resource, page) ─────► │  Paged query for projects or APIs
   │  ◄─────────── item list ───────────────── │
   │  ── get_templates(config, item_ids) ────► │  Fetches API details & converts to HTTP node templates
   │  ◄─────────── node templates ──────────── │
```

- **Transport**: Standard I/O (stdin/stdout), line-delimited JSON-RPC 2.0 documents (LF framed).
- **Residency Mode**: On-demand, stateless and lightweight.
- **Drag-and-Drop Import**: Drag any Eolink API item directly into the MPE flow canvas to create fully configured HTTP request nodes with URL, query parameters, headers, JSON body schema, and sample data.

---

## 2. Project Structure

```
mpe-plugin-eolink/
├── eolink.py                 # Plugin core logic (JSON-RPC loop + Apikit client + template converter + self-test)
├── plugin.json               # Plugin manifest (entry: ./eolink, kind, capabilities, min_host_version)
├── .gitignore                # Git ignore rules (Python cache, venv, build artifacts)
├── .github/
│   └── workflows/
│       └── ci.yml            # CI test + PyInstaller cross-platform build + Release + Registry sync
├── README.md                 # Documentation in Chinese (default)
└── README_EN.md              # Documentation in English
```

---

## 3. Supported Apikit / Eolink OpenAPI Specifications

This plugin interacts with Apikit / Eolink OpenAPI v3 endpoints:
- **Project List**: `GET /v3/api-management/projects?space_id={space_id}`
- **API List**: `GET /v3/api-management/apis?project_id={project_id}&space_id={space_id}&page={page}&size={size}`
- **API Details**: `GET /v3/api-management/api?project_id={project_id}&space_id={space_id}&api_id={api_id}`

### Key Features
1. **Automatic Route Normalization**: Handles the standard Apikit `/index.php` prefix as well as any custom service address.
2. **Method Enum Mapping**: Maps `api_request_type` integer enums (0:POST, 1:GET, 2:PUT, 3:DELETE, 4:HEAD, 5:OPTIONS, 6:PATCH).
3. **Recursive Body & Param Extraction**: Recursively converts nested JSON / Form parameter structures into formatted JSON sample request bodies and structured query/header parameter lists.

---

## 4. Configuration Parameters

The following parameters can be configured in the MPE datasource settings:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `base_url` | string | Yes | none (must be provided) | Eolink / Apikit OpenAPI base address; no third-party default is built in |
| `api_key` | string | Yes | - | Apikit OpenAPI authentication token (`Eo-Secret-Key`) |
| `space_id` | string | No | - | Space ID in Apikit |

---

## 5. Local Development & Testing

### Running the Offline Self-Test Suite
The plugin includes a built-in Mock self-test suite covering `describe`, `validate_config`, `browse`, `get_item`, and `get_templates`:

```bash
# Run from source
python3 eolink.py --self-test

# Or run compiled binary
./dist/eolink --self-test
```

Example Output:
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
Start the JSON-RPC event loop:
```bash
python3 eolink.py
```
Send a JSON-RPC request line:
```json
{"jsonrpc":"2.0","id":1,"method":"describe","params":{}}
```

---

## 6. Build & Release Workflow

When pushing a version tag (e.g. `v0.1.1`) to this repository, GitHub Actions will automatically:
1. Run `eolink.py --self-test`;
2. Compile standalone native binaries across platforms (Ubuntu, Windows, macOS) using PyInstaller;
3. Package release bundles (`eolink-<version>-<platform>.zip` with `.sha256` digests);
4. Create a GitHub Release;
5. Trigger the `multi-protocol-flow/mpe-plugin-registry` repository workflow to automatically synchronize the marketplace index.

## License

MIT — see [LICENSE](./LICENSE).
