#!/usr/bin/env python3
"""Eolink / Apikit 数据源插件（只读浏览 + 拖拽导入）

支持 Apikit OpenAPI v3 规范：
- 项目列表：GET /v3/api-management/projects?space_id={space_id}
- API 列表：GET /v3/api-management/apis?project_id={project_id}&space_id={space_id}&page={page}&size={size}
- API 详情：GET /v3/api-management/api?project_id={project_id}&space_id={space_id}&api_id={api_id}
- 支持自定义 base_url（默认 http://uat.apikit.com/index.php）
- 支持直接配置 project_id（直接定位项目）与 space_id
- 自动适配 /index.php 路由前缀与 api_request_type 整数枚举（0:POST, 1:GET, 2:PUT, 3:DELETE...）
- 自动将 API 详情完整转换为画布节点模板（请求路径、查询参数、请求头、JSON/表单请求体、认证等）

提供 --self-test 命令行自测模式。
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_EOLINK_BASE = "http://uat.apikit.com/index.php"

# Apikit api_request_type 整数枚举映射
REQUEST_TYPE_MAP = {
    0: "POST",
    1: "GET",
    2: "PUT",
    3: "DELETE",
    4: "HEAD",
    5: "OPTIONS",
    6: "PATCH",
}

# 接口状态映射
API_STATUS_MAP = {
    0: "已发布",
    1: "维护",
    2: "已废弃",
    3: "待确定",
    4: "开发",
    5: "测试",
    6: "对接",
    7: "异常",
    8: "设计中",
    9: "完成",
}

# 协议类型映射
API_PROTOCOL_MAP = {
    0: "HTTP",
    1: "HTTPS",
    2: "WS",
    3: "WSS",
    4: "TCP",
    5: "UDP",
}

# 请求参数类型映射
PARAM_TYPE_MAP = {
    0: "FORM-DATA",
    1: "RAW",
    2: "JSON",
    3: "XML",
    4: "Binary",
    5: "x-www-form-urlencoded",
}

DATASOURCE_DESCRIPTION = {
    "id": "eolink",
    "display_name": "Eolink",
    "auth": {"type": "api_key"},
    "config_schema": {
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "title": "服务地址 (Base URL，默认 http://uat.apikit.com/index.php)",
            },
            "api_key": {
                "type": "string",
                "format": "password",
                "title": "Eo-Secret-Key (个人令牌)",
            },
            "space_id": {
                "type": "string",
                "title": "空间 ID (space_id，可选)",
            },
            "project_id": {
                "type": "string",
                "title": "项目 ID (project_id，可选；填入后直接定位该项目)",
            },
        },
        "required": ["api_key"],
    },
    "resources": [
        {
            "id": "project",
            "label": "项目",
            "children": "api",
            "leaf": False,
        },
        {
            "id": "api",
            "label": "接口",
            "children": None,
            "leaf": True,
        },
    ],
}


def parse_http_method(raw: Dict[str, Any]) -> Optional[str]:
    """解析 HTTP Method，支持 api_request_type 整数与 api_method 字符串"""
    req_type = raw.get("api_request_type")
    if req_type is None:
        req_type = raw.get("apiRequestType")

    if req_type is not None:
        if isinstance(req_type, int) and req_type in REQUEST_TYPE_MAP:
            return REQUEST_TYPE_MAP[req_type]
        if isinstance(req_type, str):
            if req_type.isdigit() and int(req_type) in REQUEST_TYPE_MAP:
                return REQUEST_TYPE_MAP[int(req_type)]
            if req_type.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
                return req_type.upper()

    method = raw.get("api_method") or raw.get("method") or raw.get("apiMethod")
    if method:
        return str(method).upper()
    return None


def normalize_api_raw(raw: Any, fallback_aid: str = "") -> Dict[str, Any]:
    """标准化 API 原始对象，展开 baseInfo/api_info 等嵌套字段"""
    if not isinstance(raw, dict):
        return {"api_id": fallback_aid}

    merged = dict(raw)
    for sub_key in ("baseInfo", "base_info", "apiInfo", "api_info"):
        sub = raw.get(sub_key)
        if isinstance(sub, dict):
            for k, v in sub.items():
                if k not in merged or merged[k] is None or merged[k] == "":
                    merged[k] = v
    return merged


def build_sample_value(param: Dict[str, Any]) -> Any:
    """根据 Eolink/Apikit 参数定义递归构建示例值"""
    p_type = str(param.get("paramType") if param.get("paramType") is not None else (param.get("param_type") or "0"))
    p_value = param.get("paramValue") if param.get("paramValue") is not None else param.get("param_value")
    children = param.get("childList") or param.get("child_list") or []

    # 若已包含有效 JSON 字符串，优先解析
    if isinstance(p_value, str) and (p_value.strip().startswith("{") or p_value.strip().startswith("[")):
        try:
            return json.loads(p_value)
        except Exception:
            pass

    # Array: 12 or 'array'
    if p_type in ("12", "array"):
        if children:
            return [build_sample_value(c) for c in children]
        return []

    # Object: 13 or 'object' 或包含子字段
    if p_type in ("13", "object") or children:
        obj = {}
        for c in children:
            key = c.get("paramKey") or c.get("param_key") or ""
            if key and not key.startswith("item["):
                obj[key] = build_sample_value(c)
            elif len(children) == 1 and key.startswith("item["):
                return build_sample_value(c)
        if obj:
            return obj

    # Number / Int / Long: 3, 4, 5, 10, 11, 14
    if p_type in ("3", "4", "5", "10", "11", "14", "int", "float", "double", "number", "long"):
        if p_value is not None and p_value != "":
            try:
                if "." in str(p_value):
                    return float(p_value)
                return int(p_value)
            except Exception:
                pass
        return 0

    # Boolean: 8 or 'boolean' / 'bool'
    if p_type in ("8", "boolean", "bool"):
        if str(p_value).lower() in ("true", "1"):
            return True
        return False

    # Null: 15
    if p_type in ("15", "null"):
        return None

    # String / default
    if p_value is not None and p_value != "":
        return p_value
    name = param.get("paramName") or param.get("param_name") or ""
    return name


def build_json_body_from_request_info(request_info: List[Dict[str, Any]]) -> str:
    """从 request_info 递归构建格式化的 JSON 请求体"""
    if not request_info:
        return ""
    root = {}
    for p in request_info:
        key = p.get("paramKey") or p.get("param_key")
        if key:
            root[key] = build_sample_value(p)
    return json.dumps(root, ensure_ascii=False, indent=2) if root else ""


def convert_api_to_template(item_id: str, raw: Dict[str, Any], base_url_config: str = "") -> Dict[str, Any]:
    """将 Eolink API 详情数据转换为标准 NodeTemplate"""
    norm_raw = normalize_api_raw(raw, item_id.split(":")[-1])

    name = norm_raw.get("api_name") or norm_raw.get("name") or norm_raw.get("apiName") or "HTTP Request"
    method_upper = parse_http_method(norm_raw) or "GET"
    method_lower = method_upper.lower()

    path = norm_raw.get("api_uri") or norm_raw.get("path") or norm_raw.get("uri") or norm_raw.get("apiURI") or ""

    # Protocol
    proto_raw = norm_raw.get("api_protocol")
    if proto_raw is None:
        proto_raw = norm_raw.get("apiProtocol")
    type_id = "http"
    if proto_raw in (2, 3) or str(proto_raw) in ("2", "3") or path.startswith("ws://") or path.startswith("wss://"):
        type_id = "ws_connect"

    # Query Params (url_param / urlParam)
    url_params = norm_raw.get("url_param") or norm_raw.get("urlParam") or []
    query_parts = []
    for up in url_params:
        k = up.get("param_key") or up.get("paramKey") or ""
        v = up.get("param_value") if up.get("param_value") is not None else up.get("paramValue")
        if k:
            if v is not None and v != "":
                query_parts.append(f"{k}={v}")
            else:
                query_parts.append(f"{k}={{{{{k}}}}}")

    full_url = path
    if query_parts:
        sep = "&" if "?" in full_url else "?"
        full_url += sep + "&".join(query_parts)

    if not full_url.startswith("http://") and not full_url.startswith("https://") and not full_url.startswith("ws://") and not full_url.startswith("wss://"):
        base = norm_raw.get("base_url") or norm_raw.get("baseUrl") or norm_raw.get("host")
        if base:
            full_url = f"{str(base).rstrip('/')}/{full_url.lstrip('/')}"
        elif base_url_config and not base_url_config.endswith("/index.php") and "apikit.com" not in base_url_config:
            full_url = f"{str(base_url_config).rstrip('/')}/{full_url.lstrip('/')}"

    # Headers (header_info / headerInfo)
    headers: Dict[str, str] = {}
    header_list = norm_raw.get("header_info") or norm_raw.get("headerInfo") or []
    for h in header_list:
        hk = h.get("header_name") or h.get("headerName") or h.get("param_key") or h.get("paramKey") or h.get("name")
        hv = h.get("header_value") if h.get("header_value") is not None else (h.get("headerValue") if h.get("headerValue") is not None else (h.get("param_value") if h.get("param_value") is not None else h.get("paramValue")))
        if hk:
            headers[hk] = str(hv) if (hv is not None and hv != "") else f"{{{{{hk}}}}}"

    # Body & Body Type & Content Type
    req_param_type = norm_raw.get("api_request_param_type")
    if req_param_type is None:
        req_param_type = norm_raw.get("apiRequestParamType")
    if req_param_type is not None:
        try:
            req_param_type = int(req_param_type)
        except Exception:
            pass

    body_type = "none"
    content_type = "json"
    body = ""
    form_fields = None
    file_path = None

    req_info = norm_raw.get("request_info") or norm_raw.get("requestInfo") or []
    raw_body_text = norm_raw.get("api_request_raw") or norm_raw.get("apiRequestRaw") or ""

    if req_param_type == 0:  # Form-data
        body_type = "form-data"
        content_type = "text"
        form_fields = []
        for p in req_info:
            k = p.get("param_key") or p.get("paramKey") or ""
            v = p.get("param_value") if p.get("param_value") is not None else p.get("paramValue")
            pt = str(p.get("param_type") if p.get("param_type") is not None else (p.get("paramType") or "0"))
            ft = "file" if pt in ("2", "file") else "text"
            if k:
                form_fields.append({"key": k, "value": str(v or ""), "field_type": ft})
    elif req_param_type == 1:  # RAW
        body_type = "body"
        content_type = "text"
        body = raw_body_text
    elif req_param_type == 2:  # JSON
        body_type = "body"
        content_type = "json"
        if raw_body_text and (raw_body_text.strip().startswith("{") or raw_body_text.strip().startswith("[")):
            body = raw_body_text
        elif req_info:
            body = build_json_body_from_request_info(req_info)
    elif req_param_type == 3:  # XML
        body_type = "body"
        content_type = "xml"
        body = raw_body_text
    elif req_param_type == 4:  # Binary
        body_type = "binary"
        file_path = norm_raw.get("api_request_binary") or norm_raw.get("apiRequestBinary") or ""
    elif req_param_type == 5:  # x-www-form-urlencoded
        body_type = "x-www-form-urlencoded"
        content_type = "text"
        form_fields = []
        for p in req_info:
            k = p.get("param_key") or p.get("paramKey") or ""
            v = p.get("param_value") if p.get("param_value") is not None else p.get("paramValue")
            if k:
                form_fields.append({"key": k, "value": str(v or ""), "field_type": "text"})
    else:
        if req_info and method_lower in ("post", "put", "patch"):
            body_type = "body"
            content_type = "json"
            body = build_json_body_from_request_info(req_info)
        elif raw_body_text:
            body_type = "body"
            content_type = "json" if (raw_body_text.strip().startswith("{") or raw_body_text.strip().startswith("[")) else "text"
            body = raw_body_text

    # Auth Info
    auth_config = None
    auth_info = norm_raw.get("auth_info") or norm_raw.get("authInfo")
    if isinstance(auth_info, dict):
        basic = auth_info.get("basicAuth")
        if isinstance(basic, dict) and (basic.get("username") or basic.get("password")):
            auth_config = {
                "auth_type": "basic",
                "username": basic.get("username", ""),
                "password": basic.get("password", ""),
            }
        jwt = auth_info.get("jwtAuth")
        if isinstance(jwt, dict) and jwt.get("payload"):
            auth_config = {
                "auth_type": "bearer",
                "token": jwt.get("payload", ""),
            }

    if type_id == "ws_connect":
        node_data = {
            "type": "ws_connect",
            "url": full_url,
        }
    else:
        node_data = {
            "type": "http",
            "url": full_url,
            "method": method_lower,
            "headers": headers if headers else None,
            "body_type": body_type,
            "content_type": content_type,
            "body": body if body else None,
            "form_fields": form_fields if form_fields else None,
            "file_path": file_path if file_path else None,
            "auth": auth_config if auth_config else None,
        }

    template_name = name if name else (f"{method_upper} {path}" if path else "HTTP Request")
    return {
        "type_id": type_id,
        "name": template_name,
        "data": {k: v for k, v in node_data.items() if v is not None},
        "source_ref": {
            "datasource": "eolink",
            "item_id": item_id,
            "updated_at": norm_raw.get("api_update_time") or norm_raw.get("apiUpdateTime"),
        },
    }


class EolinkClient:
    """Eolink / Apikit OpenAPI 客户端"""

    def __init__(self, config: Dict[str, Any]):
        raw_base = str(config.get("base_url") or "").strip()
        if not raw_base:
            self.base_url = DEFAULT_EOLINK_BASE
        else:
            cleaned = raw_base.rstrip("/")
            # 如果是 apikit 且未包含 /index.php，自动补全
            if "apikit.com" in cleaned and not cleaned.endswith("/index.php"):
                self.base_url = f"{cleaned}/index.php"
            else:
                self.base_url = cleaned

        self.api_key = str(config.get("api_key") or "").strip()
        self.project_id = str(config.get("project_id") or "").strip() if config.get("project_id") else None
        self.space_id = str(config.get("space_id") or "").strip() if config.get("space_id") else None

    def _headers(self) -> Dict[str, str]:
        return {
            "Eo-Secret-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "mpe-eolink-datasource/0.1.0",
        }

    def _request(self, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            filtered_query = {k: v for k, v in query.items() if v is not None}
            if filtered_query:
                url += "?" + urllib.parse.urlencode(filtered_query)

        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} from {url}: {err_body}") from e
        except Exception as e:
            raise RuntimeError(f"Request to {url} failed: {e}") from e

    def validate_config(self) -> Dict[str, Any]:
        if not self.api_key:
            return {"valid": False, "message": "API Key (Eo-Secret-Key) 不能为空"}
        try:
            if self.project_id:
                query = {"project_id": self.project_id, "page": 1, "size": 1}
                if self.space_id:
                    query["space_id"] = self.space_id
                self._request("/v3/api-management/apis", query=query)
            else:
                query = {"space_id": self.space_id} if self.space_id else None
                try:
                    self._request("/v3/api-management/projects", query=query)
                except Exception:
                    self._request("/projects", query=query)
            return {"valid": True, "message": None}
        except Exception as e:
            return {"valid": False, "message": str(e)}

    def list_projects(self) -> List[Dict[str, Any]]:
        if self.project_id:
            return [{
                "id": self.project_id,
                "name": f"项目 ({self.project_id})",
                "protocol": "other",
                "method": None,
                "path": None,
                "summary": "配置指定的项目",
                "extra": [{"label": "项目 ID", "value": self.project_id}],
                "raw": {"project_id": self.project_id},
            }]

        query = {"space_id": self.space_id} if self.space_id else None
        res: Any = None
        try:
            res = self._request("/v3/api-management/projects", query=query)
        except Exception:
            try:
                res = self._request("/projects", query=query)
            except Exception:
                return []

        items = []
        if isinstance(res, dict):
            data = res.get("data")
            if isinstance(data, dict):
                items = data.get("items") or data.get("list") or []
            elif isinstance(data, list):
                items = data
        elif isinstance(res, list):
            items = res

        result = []
        for item in items:
            p_id = str(item.get("project_id") or item.get("id") or item.get("projectID") or item.get("projectHashKey") or "")
            p_name = item.get("project_name") or item.get("name") or item.get("projectName") or "未命名项目"
            desc = item.get("project_desc") or item.get("description") or item.get("project_description")
            result.append({
                "id": p_id,
                "name": p_name,
                "protocol": "other",
                "method": None,
                "path": None,
                "summary": desc,
                "extra": [{"label": "项目 ID", "value": p_id}] if p_id else [],
                "raw": item,
            })
        return result

    def list_apis(self, project_id: str, page: int = 1, size: int = 100) -> List[Dict[str, Any]]:
        # 默认单页大容量（1000 条），并自动轮询后续页直至取完该项目全部接口
        fetch_size = max(size, 1000)
        curr_page = page
        all_raw_items: List[Dict[str, Any]] = []

        while True:
            query: Dict[str, Any] = {
                "project_id": project_id,
                "page": curr_page,
                "size": fetch_size,
            }
            if self.space_id:
                query["space_id"] = self.space_id

            res = self._request("/v3/api-management/apis", query=query)
            paginator: Optional[Dict[str, Any]] = None
            page_items: List[Dict[str, Any]] = []

            if isinstance(res, dict):
                data = res.get("data")
                if isinstance(data, dict):
                    page_items = data.get("items") or data.get("list") or data.get("apiList") or []
                    paginator = data.get("paginator")
                elif isinstance(data, list):
                    page_items = data
            elif isinstance(res, list):
                page_items = res

            if not page_items:
                break

            all_raw_items.extend(page_items)

            # 判断是否有下一页
            if paginator and isinstance(paginator, dict):
                total = paginator.get("total") or 0
                if len(all_raw_items) >= total:
                    break
                curr_page += 1
                if curr_page > 10:  # 安全上限：最多拉取 10,000 条
                    break
            else:
                break

        items = all_raw_items
        result = []
        for item in items:
            raw_id = str(item.get("api_id") or item.get("id") or item.get("apiID") or "")
            item_id = f"{project_id}:{raw_id}" if raw_id else f"{project_id}:unknown"
            name = item.get("api_name") or item.get("name") or item.get("apiName") or "未命名接口"
            method = parse_http_method(item)
            path = item.get("api_uri") or item.get("path") or item.get("uri") or item.get("apiURI")

            protocol = "http" if method else "other"
            extra = []
            group = item.get("group_name") or item.get("groupName")
            if group:
                extra.append({"label": "分组", "value": str(group)})
            status = item.get("api_status") if item.get("api_status") is not None else item.get("status")
            if status is not None:
                status_desc = API_STATUS_MAP.get(status, str(status)) if isinstance(status, int) else str(status)
                extra.append({"label": "状态", "value": status_desc})

            result.append({
                "id": item_id,
                "name": name,
                "protocol": protocol,
                "method": method,
                "path": str(path) if path else None,
                "summary": item.get("summary") or item.get("description"),
                "extra": extra,
                "raw": item,
            })
        return result

    def get_api_detail(self, item_id: str) -> Dict[str, Any]:
        parts = item_id.split(":", 1)
        if len(parts) == 2:
            pid, aid = parts[0], parts[1]
        else:
            pid, aid = self.project_id or "", parts[0]

        query = {"project_id": pid, "api_id": aid}
        if self.space_id:
            query["space_id"] = self.space_id

        try:
            # OpenAPI v3 详情接口：/v3/api-management/api
            res = self._request("/v3/api-management/api", query=query)
        except Exception:
            # 回退调用
            res = self._request(f"/projects/{pid}/apis/{aid}")

        raw = res.get("data") if isinstance(res, dict) and "data" in res else res
        norm_raw = normalize_api_raw(raw, aid)

        name = norm_raw.get("api_name") or norm_raw.get("name") or norm_raw.get("apiName") or "未命名接口"
        method = parse_http_method(norm_raw)
        path = norm_raw.get("api_uri") or norm_raw.get("path") or norm_raw.get("uri") or norm_raw.get("apiURI")

        extra = []
        group = norm_raw.get("group_name") or norm_raw.get("groupName")
        if group:
            extra.append({"label": "分组", "value": str(group)})
        status = norm_raw.get("api_status") if norm_raw.get("api_status") is not None else norm_raw.get("apiStatus")
        if status is not None:
            status_desc = API_STATUS_MAP.get(status, str(status)) if isinstance(status, int) else str(status)
            extra.append({"label": "状态", "value": status_desc})
        protocol = norm_raw.get("api_protocol") if norm_raw.get("api_protocol") is not None else norm_raw.get("apiProtocol")
        if protocol is not None:
            proto_desc = API_PROTOCOL_MAP.get(protocol, str(protocol)) if isinstance(protocol, int) else str(protocol)
            extra.append({"label": "协议", "value": proto_desc})
        update_time = norm_raw.get("api_update_time") or norm_raw.get("apiUpdateTime")
        if update_time:
            extra.append({"label": "更新时间", "value": str(update_time)})

        return {
            "id": item_id,
            "name": name,
            "protocol": "http" if method else "other",
            "method": method,
            "path": str(path) if path else None,
            "summary": norm_raw.get("summary") or norm_raw.get("description") or norm_raw.get("api_note_raw") or norm_raw.get("apiNoteRaw") or norm_raw.get("api_note"),
            "extra": extra,
            "raw": norm_raw,
        }


def handle_browse(config: Dict[str, Any], resource_id: str, parent_id: Optional[str], page: int, size: int) -> List[Dict[str, Any]]:
    client = EolinkClient(config)
    if resource_id == "project":
        return client.list_projects()
    elif resource_id == "api":
        pid = parent_id or client.project_id
        if not pid:
            raise ValueError("browse(api) 缺少 parent_id (project_id)")
        return client.list_apis(pid, page=page, size=size)
    else:
        raise ValueError(f"未知资源类型: {resource_id}")


def handle_get_item(config: Dict[str, Any], resource_id: str, item_id: str) -> Dict[str, Any]:
    client = EolinkClient(config)
    if resource_id == "api":
        return client.get_api_detail(item_id)
    elif resource_id == "project":
        return {
            "id": item_id,
            "name": f"Project {item_id}",
            "protocol": "other",
            "method": None,
            "path": None,
            "summary": None,
            "extra": [],
            "raw": {"project_id": item_id},
        }
    else:
        raise ValueError(f"未知资源类型: {resource_id}")


def handle_get_templates(config: Dict[str, Any], item_ids: List[str]) -> List[Dict[str, Any]]:
    client = EolinkClient(config)
    templates = []
    for item_id in item_ids:
        try:
            detail = client.get_api_detail(item_id)
            raw = detail.get("raw", {})
            template = convert_api_to_template(item_id, raw, base_url_config=client.base_url)
            templates.append(template)
        except Exception as e:
            sys.stderr.write(f"eolink get_templates warning for {item_id}: {e}\n")
    return templates


def handle_validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    client = EolinkClient(config)
    return client.validate_config()


def describe_result() -> Dict[str, Any]:
    return {
        "plugin": {
            "name": "eolink",
            "version": "0.1.0",
            "kind": "datasource",
            "description": "Eolink 数据源（只读浏览 + 拖拽导入）",
            "entry": {"command": "python3", "args": ["./eolink.py"]},
            "capabilities": {"streaming": False},
        },
        "nodes": [],
        "datasources": [DATASOURCE_DESCRIPTION],
    }


def run_stdio_loop():
    """标准 JSON-RPC 2.0 stdio 事件循环"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"eolink: malformed json frame: {e}\n")
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {}) or {}

        if method == "describe":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": describe_result()}
        elif method == "browse":
            try:
                config = params.get("config", {})
                resource_id = params.get("resource_id", "project")
                parent_id = params.get("parent_id")
                page = params.get("page", 1)
                size = params.get("size", 100)
                items = handle_browse(config, resource_id, parent_id, page, size)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": items}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "get_item":
            try:
                config = params.get("config", {})
                resource_id = params.get("resource_id", "api")
                item_id = params.get("item_id", "")
                item = handle_get_item(config, resource_id, item_id)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": item}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "get_templates":
            try:
                config = params.get("config", {})
                item_ids = params.get("item_ids", [])
                templates = handle_get_templates(config, item_ids)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": templates}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "validate_config":
            try:
                config = params.get("config", {})
                res = handle_validate_config(config)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}
        elif method == "shutdown":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": None}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            break
        else:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def run_self_test() -> int:
    """--self-test 模式：使用 Mock 数据测试全流程，打印 PASS/FAIL"""
    print("[Self-Test] Starting Eolink datasource plugin self-test...")
    passed = True

    # 1. Test describe
    try:
        desc = describe_result()
        assert desc["plugin"]["name"] == "eolink"
        assert desc["plugin"]["kind"] == "datasource"
        assert len(desc["datasources"]) == 1
        assert desc["datasources"][0]["id"] == "eolink"
        assert len(desc["datasources"][0]["resources"]) == 2
        print("  ✓ describe test passed")
    except Exception as e:
        print(f"  ✗ describe test failed: {e}")
        passed = False

    # 2. Test validate_config (empty api_key failure)
    try:
        res = handle_validate_config({})
        assert res["valid"] is False
        assert res["message"] is not None
        print("  ✓ validate_config (empty) test passed")
    except Exception as e:
        print(f"  ✗ validate_config test failed: {e}")
        passed = False

    # 3. Test mock data mapping for Apikit v3
    class MockClient(EolinkClient):
        def _request(self, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
            if path == "/v3/api-management/projects":
                return {
                    "type": "array",
                    "data": [
                        {
                            "project_id": "proj_1001",
                            "project_name": "用户中心",
                            "project_desc": "User Center API",
                        }
                    ],
                }
            elif path == "/v3/api-management/apis":
                return {
                    "statusCode": "000000",
                    "data": {
                        "paginator": {"page": 1, "size": 20, "total": 1},
                        "items": [
                            {
                                "api_id": 237665,
                                "api_name": "获取用户信息",
                                "api_request_type": 1,  # 1 = GET
                                "api_uri": "http://172.18.65.54:8099/users/1",
                                "group_name": "用户模块",
                                "api_status": 3,
                            }
                        ],
                    },
                }
            elif path == "/v3/api-management/api":
                return {
                    "statusCode": "000000",
                    "data": {
                        "api_id": 237665,
                        "api_name": "获取用户信息",
                        "api_request_type": 0,  # POST
                        "api_uri": "/users/{id}",
                        "api_protocol": 0,
                        "api_status": 0,
                        "api_request_param_type": 2,  # JSON
                        "header_info": [
                            {"header_name": "Authorization", "header_value": "Bearer token123"}
                        ],
                        "url_param": [
                            {"param_key": "lang", "param_value": "zh-CN"}
                        ],
                        "request_info": [
                            {
                                "param_key": "username",
                                "param_type": "0",
                                "param_value": "alice",
                                "param_name": "用户名"
                            },
                            {
                                "param_key": "details",
                                "param_type": "13",
                                "child_list": [
                                    {"param_key": "age", "param_type": "3", "param_value": "25"}
                                ]
                            }
                        ]
                    },
                }
            raise ValueError(f"Unknown mock path: {path}")

    # Test browse projects
    try:
        mock_client = MockClient({"api_key": "mock_key"})
        projects = mock_client.list_projects()
        assert len(projects) == 1
        assert projects[0]["id"] == "proj_1001"
        assert projects[0]["name"] == "用户中心"
        print("  ✓ browse(project) Apikit v3 test passed")
    except Exception as e:
        print(f"  ✗ browse(project) Apikit v3 test failed: {e}")
        passed = False

    # Test browse apis with api_request_type
    try:
        mock_client = MockClient({"api_key": "mock_key", "project_id": "proj_1001"})
        apis = mock_client.list_apis("proj_1001")
        assert len(apis) == 1
        assert apis[0]["id"] == "proj_1001:237665"
        assert apis[0]["name"] == "获取用户信息"
        assert apis[0]["protocol"] == "http"
        assert apis[0]["method"] == "GET"
        assert apis[0]["path"] == "http://172.18.65.54:8099/users/1"
        print("  ✓ browse(api) Apikit v3 test passed")
    except Exception as e:
        print(f"  ✗ browse(api) Apikit v3 test failed: {e}")
        passed = False

    # Test get_item
    try:
        detail = mock_client.get_api_detail("proj_1001:237665")
        assert detail["name"] == "获取用户信息"
        assert detail["method"] == "POST"
        assert len(detail["extra"]) >= 2
        print("  ✓ get_item Apikit v3 test passed")
    except Exception as e:
        print(f"  ✗ get_item Apikit v3 test failed: {e}")
        passed = False

    # Test get_templates
    try:
        raw = mock_client.get_api_detail("proj_1001:237665")["raw"]
        tpl = convert_api_to_template("proj_1001:237665", raw, "http://api.example.com")
        assert tpl["type_id"] == "http"
        assert tpl["name"] == "获取用户信息"
        data = tpl["data"]
        assert data["method"] == "post"
        assert data["url"] == "http://api.example.com/users/{id}?lang=zh-CN"
        assert data["headers"]["Authorization"] == "Bearer token123"
        assert data["body_type"] == "body"
        assert data["content_type"] == "json"
        body_obj = json.loads(data["body"])
        assert body_obj["username"] == "alice"
        assert body_obj["details"]["age"] == 25

        # Test form-data template
        form_raw = {
            "api_name": "上传文件",
            "api_uri": "/upload",
            "api_request_type": 0,
            "api_request_param_type": 0,  # Form-data
            "request_info": [
                {"param_key": "desc", "param_value": "test desc", "param_type": "0"},
                {"param_key": "avatar", "param_value": "", "param_type": "2"},
            ]
        }
        form_tpl = convert_api_to_template("proj_1001:237666", form_raw)
        assert form_tpl["name"] == "上传文件"
        assert form_tpl["data"]["body_type"] == "form-data"
        assert len(form_tpl["data"]["form_fields"]) == 2
        assert form_tpl["data"]["form_fields"][0]["field_type"] == "text"
        assert form_tpl["data"]["form_fields"][1]["field_type"] == "file"

        print("  ✓ get_templates (convert_api_to_template) Apikit v3 test passed")
    except Exception as e:
        print(f"  ✗ get_templates test failed: {e}")
        passed = False

    if passed:
        print("\n[Self-Test] Result: PASS")
        return 0
    else:
        print("\n[Self-Test] Result: FAIL")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Eolink / Apikit datasource plugin")
    parser.add_argument("--self-test", action="store_true", help="Run self-tests and exit")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())
    else:
        run_stdio_loop()


if __name__ == "__main__":
    main()
