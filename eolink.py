#!/usr/bin/env python3
"""Eolink / Apikit 数据源插件（只读浏览 + 拖拽导入）

支持 Apikit OpenAPI v3 规范：
- 项目列表：GET /v3/api-management/projects?space_id={space_id}
- API 列表：GET /v3/api-management/apis?project_id={project_id}&space_id={space_id}&page={page}&size={size}
- API 详情：GET /v3/api-management/api?project_id={project_id}&space_id={space_id}&api_id={api_id}
- 支持自定义 base_url（必填，不内置任何第三方默认地址）
- 支持直接配置 project_id（直接定位项目）与 space_id
- 自动适配 /index.php 路由前缀与 api_request_type 整数枚举（0:POST, 1:GET, 2:PUT, 3:DELETE...）
- 自动将 API 详情完整转换为画布节点模板（请求路径、查询参数、请求头、JSON/表单请求体、认证等）

提供 --self-test 命令行自测模式。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Ensure UTF-8 encoding across Windows, Linux and macOS
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
# base_url 由用户在数据源配置中提供，不再内置任何第三方服务地址。

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
    6: "RPC/gRPC",
    7: "FS2.0",
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
                "title": "服务地址 (Base URL，如 https://your-eolink-host/index.php)",
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
        "required": ["api_key", "base_url"],
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


def detect_api_protocol_kind(raw: Dict[str, Any]) -> str:
    """识别 Eolink 接口的真实协议类型

    返回值:
      'http' | 'https' | 'ws' | 'wss' | 'tcp' | 'udp' | 'grpc' |
      'sse' | 'graphql' | 'mqtt' | 'kafka' |
      'dubbo' | 'bolt' | 'hsf' | 'tars' | 'ufx' | 'fs20' | 'zhongzhuo' | 'other'
    """
    path = str(
        raw.get("api_uri")
        or raw.get("path")
        or raw.get("uri")
        or raw.get("apiURI")
        or ""
    ).strip()
    path_lower = path.lower()

    if path_lower.startswith("wss://"):
        return "wss"
    if path_lower.startswith("ws://"):
        return "ws"
    if path_lower.startswith("tcp://"):
        return "tcp"
    if path_lower.startswith("udp://"):
        return "udp"
    if path_lower.startswith("grpc://") or path_lower.startswith("grpcs://"):
        return "grpc"

    api_type = str(raw.get("api_type") or raw.get("apiType") or "").strip().lower()
    proto_raw = raw.get("api_protocol")
    if proto_raw is None:
        proto_raw = raw.get("apiProtocol")
    proto_str = str(proto_raw).strip().lower() if proto_raw is not None else ""

    # 1. 优先识别显式非 HTTP 的 api_type
    if api_type == "wss":
        return "wss"
    if api_type in ("websocket", "ws"):
        return "wss" if proto_str == "3" or proto_str == "wss" else "ws"
    if api_type == "tcp":
        return "tcp"
    if api_type == "udp":
        return "udp"
    if api_type == "socket":
        if proto_str in ("5", "udp"):
            return "udp"
        return "tcp"
    if api_type == "grpc":
        return "grpc"
    if api_type in ("sse", "graphql", "mqtt", "kafka", "dubbo", "bolt", "hsf", "tars", "ufx", "fs20", "zhongzhuo"):
        return api_type

    # 2. 按 api_protocol 整数/字符串枚举识别（详情接口通常不含 api_type，仅含 api_protocol）
    if proto_str in ("2", "ws", "websocket"):
        return "ws"
    if proto_str in ("3", "wss"):
        return "wss"
    if proto_str in ("4", "tcp"):
        return "tcp"
    if proto_str in ("5", "udp"):
        return "udp"
    if proto_str in ("6", "grpc", "rpc"):
        custom_info = raw.get("custom_info")
        if isinstance(custom_info, dict) and "app_name" in custom_info and "group" in custom_info:
            return "dubbo"
        return "grpc"
    if proto_str in ("7", "fs20"):
        return "fs20"
    if proto_str in ("1", "https"):
        return "https"
    if proto_str in ("0", "http") or api_type in ("http", "soap"):
        return "https" if path_lower.startswith("https://") else "http"

    if parse_http_method(raw):
        return "https" if path_lower.startswith("https://") else "http"
    return "other"


def resolve_data_item_protocol_and_method(raw: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """映射为宿主 DataItem 契约的 (protocol, method)

    DataItemProtocol 合法枚举值:
      'http' | 'websocket' | 'sse' | 'tcp' | 'graphql' | 'mqtt' | 'kafka' | 'other'
    """
    kind = detect_api_protocol_kind(raw)
    if kind in ("http", "https"):
        return "http", parse_http_method(raw) or "GET"
    if kind == "ws":
        return "websocket", "WS"
    if kind == "wss":
        return "websocket", "WSS"
    if kind == "tcp":
        return "tcp", "TCP"
    if kind == "udp":
        return "other", "UDP"
    if kind == "grpc":
        return "other", "gRPC"
    if kind == "sse":
        return "sse", "SSE"
    if kind == "graphql":
        return "graphql", "GQL"
    if kind == "mqtt":
        return "mqtt", "MQTT"
    if kind == "kafka":
        return "kafka", "KAFKA"
    if kind != "other":
        return "other", kind.upper()
    return "other", None


def parse_host_port(addr: str, default_port: int = 0) -> Tuple[str, int]:
    """从形如 '42.194.220.130:5002' 或 'tcp://127.0.0.1:9000/path' 提取 (host, port)"""
    cleaned = str(addr or "").strip()
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
    cleaned = cleaned.split("/", 1)[0].strip()
    if ":" in cleaned:
        host_part, port_part = cleaned.rsplit(":", 1)
        try:
            return host_part.strip(), int(port_part.strip())
        except Exception:
            return cleaned, default_port
    return cleaned, default_port


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


def extract_structure_map(raw: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """从详情对象提取 data_structure_list 映射: structure_id -> structure_data 参数列表"""
    ds_raw = raw.get("data_structure_list") or raw.get("dataStructureList")
    result: Dict[str, List[Dict[str, Any]]] = {}
    if isinstance(ds_raw, dict):
        for k, v in ds_raw.items():
            if isinstance(v, dict):
                sdata = v.get("structure_data") or v.get("structureData")
                if isinstance(sdata, list):
                    result[str(k)] = sdata
                    sid = v.get("structure_id") or v.get("structureID")
                    if sid is not None:
                        result[str(sid)] = sdata
    elif isinstance(ds_raw, list):
        for v in ds_raw:
            if isinstance(v, dict):
                sid = v.get("structure_id") or v.get("structureID")
                sdata = v.get("structure_data") or v.get("structureData")
                if sid is not None and isinstance(sdata, list):
                    result[str(sid)] = sdata
    return result


def expand_param_list(
    params: List[Dict[str, Any]],
    structures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    visited: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """展开列表中包含 structure_id 引用的条目"""
    if not params:
        return []
    if not structures:
        return params
    visited_set = set(visited) if visited else set()
    expanded: List[Dict[str, Any]] = []
    for p in params:
        if not isinstance(p, dict):
            continue
        key = p.get("paramKey") or p.get("param_key")
        sid = p.get("structure_id") or p.get("structureID")
        if not key and sid is not None and str(sid) in structures and str(sid) not in visited_set:
            next_visited = set(visited_set)
            next_visited.add(str(sid))
            expanded.extend(expand_param_list(structures[str(sid)], structures, next_visited))
        else:
            expanded.append(p)
    return expanded


def build_sample_value(
    param: Dict[str, Any],
    structures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    visited: Optional[set] = None,
) -> Any:
    """根据 Eolink/Apikit 参数定义递归构建示例值"""
    p_type = str(param.get("paramType") if param.get("paramType") is not None else (param.get("param_type") or "0"))
    p_value = param.get("paramValue") if param.get("paramValue") is not None else param.get("param_value")
    raw_children = param.get("childList") or param.get("child_list") or []
    visited_set = set(visited) if visited else set()

    # 如果 param_type 本身指向某个 data_structure_list ID
    if not raw_children and structures and p_type in structures and p_type not in visited_set:
        visited_set = set(visited_set)
        visited_set.add(p_type)
        raw_children = structures[p_type]

    children = expand_param_list(raw_children, structures, visited_set)

    # 若已包含有效 JSON 字符串，优先解析
    if isinstance(p_value, str) and (p_value.strip().startswith("{") or p_value.strip().startswith("[")):
        try:
            return json.loads(p_value)
        except Exception:
            pass

    # Array: 12 or 'array'
    if p_type in ("12", "array"):
        if children:
            if len(children) == 1 and str(children[0].get("paramKey") or children[0].get("param_key") or "").startswith("item["):
                return [build_sample_value(children[0], structures, visited_set)]
            return [build_sample_value(c, structures, visited_set) for c in children]
        return []

    # Object / Message: 13 or 'object' or 'message' 或包含子字段
    if p_type in ("13", "object", "message") or children:
        obj = {}
        for c in children:
            key = c.get("paramKey") or c.get("param_key") or ""
            if key and not key.startswith("item["):
                obj[key] = build_sample_value(c, structures, visited_set)
            elif len(children) == 1 and key.startswith("item["):
                return build_sample_value(c, structures, visited_set)
        if obj:
            return obj
        if p_type in ("13", "object", "message"):
            return {}

    # Number / Int / Long / Protobuf numeric types: 3, 4, 5, 10, 11, 14
    if p_type in (
        "3", "4", "5", "10", "11", "14",
        "int", "int32", "int64", "uint32", "uint64", "sint32", "sint64",
        "fixed32", "fixed64", "sfixed32", "sfixed64",
        "float", "double", "number", "long", "short", "byte",
    ):
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


def build_json_body_from_request_info(
    request_info: List[Dict[str, Any]],
    structures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    unwrap_single_message: bool = False,
) -> str:
    """从 request_info 递归构建格式化的 JSON 请求体"""
    if not request_info:
        return ""
    expanded = expand_param_list(request_info, structures)
    if (
        unwrap_single_message
        and len(expanded) == 1
        and str(expanded[0].get("param_type") or expanded[0].get("paramType") or "") == "message"
    ):
        val = build_sample_value(expanded[0], structures)
        if isinstance(val, dict):
            return json.dumps(val, ensure_ascii=False, indent=2)
    root = {}
    for p in expanded:
        key = p.get("paramKey") or p.get("param_key")
        if key:
            root[key] = build_sample_value(p, structures)
    return json.dumps(root, ensure_ascii=False, indent=2) if root else ""


def convert_api_to_template(item_id: str, raw: Dict[str, Any], base_url_config: str = "") -> Dict[str, Any]:
    """将 Eolink API 详情数据转换为标准 NodeTemplate"""
    norm_raw = normalize_api_raw(raw, item_id.split(":")[-1])
    structures = extract_structure_map(norm_raw)

    name = norm_raw.get("api_name") or norm_raw.get("name") or norm_raw.get("apiName") or "HTTP Request"
    method_upper = parse_http_method(norm_raw) or "GET"
    method_lower = method_upper.lower()

    path = str(
        norm_raw.get("api_uri")
        or norm_raw.get("path")
        or norm_raw.get("uri")
        or norm_raw.get("apiURI")
        or ""
    ).strip()

    proto_kind = detect_api_protocol_kind(norm_raw)

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

    if (
        proto_kind in ("http", "https", "ws", "wss", "sse")
        and not full_url.startswith(("http://", "https://", "ws://", "wss://"))
    ):
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
        hv = (
            h.get("header_value")
            if h.get("header_value") is not None
            else (
                h.get("headerValue")
                if h.get("headerValue") is not None
                else (h.get("param_value") if h.get("param_value") is not None else h.get("paramValue"))
            )
        )
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
        built_fields = []
        for p in expand_param_list(req_info, structures):
            k = p.get("param_key") or p.get("paramKey") or ""
            v = p.get("param_value") if p.get("param_value") is not None else p.get("paramValue")
            pt = str(p.get("param_type") if p.get("param_type") is not None else (p.get("paramType") or "0"))
            ft = "file" if pt in ("2", "file") else "text"
            if k:
                built_fields.append({"key": k, "value": str(v or ""), "field_type": ft})
        if built_fields:
            body_type = "form-data"
            content_type = "text"
            form_fields = built_fields
    elif req_param_type == 1:  # RAW
        if raw_body_text:
            body_type = "body"
            content_type = "text"
            body = raw_body_text
    elif req_param_type == 2:  # JSON
        if raw_body_text and (raw_body_text.strip().startswith("{") or raw_body_text.strip().startswith("[")):
            body = raw_body_text
        elif req_info:
            body = build_json_body_from_request_info(
                req_info,
                structures,
                unwrap_single_message=(proto_kind in ("grpc", "dubbo", "bolt", "hsf")),
            )
        if body or method_lower in ("post", "put", "patch"):
            body_type = "body"
            content_type = "json"
    elif req_param_type == 3:  # XML
        xml_body = raw_body_text or build_json_body_from_request_info(req_info, structures)
        if xml_body or method_lower in ("post", "put", "patch"):
            body_type = "body"
            content_type = "xml"
            body = xml_body
    elif req_param_type == 4:  # Binary
        bin_path = norm_raw.get("api_request_binary") or norm_raw.get("apiRequestBinary") or ""
        if bin_path or method_lower in ("post", "put", "patch"):
            body_type = "binary"
            file_path = bin_path
    elif req_param_type == 5:  # x-www-form-urlencoded
        built_fields = []
        for p in expand_param_list(req_info, structures):
            k = p.get("param_key") or p.get("paramKey") or ""
            v = p.get("param_value") if p.get("param_value") is not None else p.get("paramValue")
            if k:
                built_fields.append({"key": k, "value": str(v or ""), "field_type": "text"})
        if built_fields:
            body_type = "x-www-form-urlencoded"
            content_type = "text"
            form_fields = built_fields
    else:
        if req_info and method_lower in ("post", "put", "patch"):
            body_type = "body"
            content_type = "json"
            body = build_json_body_from_request_info(req_info, structures)
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

    if proto_kind in ("ws", "wss"):
        type_id = "ws:connect"
        ws_url = full_url
        scheme = "wss://" if proto_kind == "wss" else "ws://"
        if ws_url.startswith("http://"):
            ws_url = scheme + ws_url[len("http://"):]
        elif ws_url.startswith("https://"):
            ws_url = "wss://" + ws_url[len("https://"):]
        elif ws_url and not ws_url.startswith(("ws://", "wss://", "/", "{{")):
            ws_url = scheme + ws_url
        ws_headers = [{"key": k, "value": v, "field_type": "text"} for k, v in headers.items()] if headers else None
        node_data: Dict[str, Any] = {
            "type": "ws:connect",
            "url": ws_url,
            "headers": ws_headers,
            "use_tls": True if (proto_kind == "wss" or ws_url.startswith("wss://")) else None,
        }
    elif proto_kind == "tcp":
        type_id = "tcp:connect"
        host, port = parse_host_port(path, 0)
        node_data = {
            "type": "tcp:connect",
            "host": host,
            "port": port,
        }
    elif proto_kind == "udp":
        type_id = "udp:send_to"
        target_addr, target_port = parse_host_port(path, 0)
        udp_payload = body or raw_body_text or build_json_body_from_request_info(req_info, structures)
        node_data = {
            "type": "udp:send_to",
            "connection_id": "",
            "target_addr": target_addr,
            "target_port": target_port,
            "data": udp_payload,
            "data_encoding": "text",
        }
    elif proto_kind in ("grpc", "dubbo", "bolt", "hsf"):
        type_id = "grpc:call"
        custom_info = norm_raw.get("custom_info") if isinstance(norm_raw.get("custom_info"), dict) else {}
        service_name = str(custom_info.get("interface_name") or norm_raw.get("interface_name") or "").strip()
        method_name = str(custom_info.get("method_name") or norm_raw.get("method_name") or "").strip()
        if not service_name or not method_name:
            cleaned_uri = path.strip("/")
            if "/" in cleaned_uri:
                s_part, m_part = cleaned_uri.rsplit("/", 1)
                service_name = service_name or s_part
                method_name = method_name or m_part
            elif "." in cleaned_uri:
                s_part, m_part = cleaned_uri.rsplit(".", 1)
                service_name = service_name or s_part
                method_name = method_name or m_part
            else:
                method_name = method_name or cleaned_uri or name
        grpc_payload = body or raw_body_text or build_json_body_from_request_info(
            req_info, structures, unwrap_single_message=True
        )
        grpc_meta = [{"key": k, "value": v} for k, v in headers.items()] if headers else None
        node_data = {
            "type": "grpc:call",
            "connection_id": "",
            "service_name": service_name,
            "method_name": method_name,
            "request_json": grpc_payload or "{}",
            "timeout_ms": 30000,
            "metadata": grpc_meta,
        }
    elif proto_kind == "sse":
        type_id = "sse:connect"
        node_data = {
            "type": "sse:connect",
            "url": full_url,
            "headers": headers if headers else None,
        }
    elif proto_kind == "graphql":
        type_id = "graphql:query"
        node_data = {
            "type": "graphql:query",
            "connection_id": "",
            "query": body or raw_body_text or "",
        }
    else:
        type_id = "http:request"
        node_data = {
            "type": "http:request",
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
            # 不内置任何第三方默认地址：缺省必须报错，而不是静默连到厂商测试环境。
            self.base_url = ""
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
        if not self.base_url:
            raise RuntimeError(
                "缺少 base_url：请在数据源配置中填写 Eolink / Apikit 服务地址"
            )
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
    def get_group_maps(self, project_id: str) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
        """获取项目接口分组映射:
        - id_to_name: group_id -> group_name (如 "1906207" -> "系统管理")
        - id_to_path: group_id -> full_path (如 "1906268" -> "系统管理/用户管理/租户管理")
        - name_to_path: group_name -> full_path
        """
        query: Dict[str, Any] = {
            "project_id": project_id,
            "project_hash_key": project_id,
            "projectHashKey": project_id,
        }
        if self.space_id:
            query["space_id"] = self.space_id

        child_keys = (
            "childGroupList", "child_group_list",
            "children", "childList", "child_list",
            "sub_groups", "subGroups",
            "subGroupList", "sub_group_list",
            "groupList", "group_list",
        )

        def extract_children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
            for k in child_keys:
                c = node.get(k)
                if isinstance(c, list) and c:
                    return c
            return []

        def extract_gid_and_name(node: Dict[str, Any]) -> Tuple[Optional[str], str]:
            gid = None
            for k in ("group_id", "groupID", "api_group_id", "apiGroupID", "id"):
                if node.get(k) is not None:
                    gid = str(node[k])
                    break
            gname = ""
            for k in ("group_name", "groupName", "api_group_name", "apiGroupName", "name"):
                if node.get(k):
                    gname = str(node[k]).strip()
                    break
            return gid, gname

        def extract_pid(node: Dict[str, Any]) -> str:
            for k in ("parent_group_id", "parentGroupID", "parent_id", "parentID", "parent"):
                if node.get(k) is not None:
                    return str(node[k])
            return "0"

        all_group_roots: List[Dict[str, Any]] = []

        for endpoint in (
            "/v3/api-management/api/groups",
            "/v3/api-management/api-groups",
            "/v3/api-management/api-group/list",
            "/v3/api-management/groups",
            "/v3/api-management/group/list",
            "/v3/api-management/api/group/list",
            "/v3/api-management/group/get-group-list",
            "/api-management/api-group/get-group-list",
            "/api-management/group/get-group-list",
            "/api-management/group/list",
            f"/projects/{project_id}/api-groups",
            f"/projects/{project_id}/groups",
            "/api-management/groups",
            "/v3/api-management/project",
            f"/projects/{project_id}",
        ):
            try:
                res = self._request(endpoint, query=query)
                if res and isinstance(res, (dict, list)):
                    raw_data = res.get("data") if isinstance(res, dict) and "data" in res else res
                    if isinstance(raw_data, list) and raw_data:
                        all_group_roots = raw_data
                        break
                    elif isinstance(raw_data, dict):
                        for lk in ("groupList", "group_list", "items", "list", "apiGroupList", "childGroupList", "groups"):
                            sub = raw_data.get(lk)
                            if isinstance(sub, list) and sub:
                                all_group_roots = sub
                                break
                        if all_group_roots:
                            break
            except Exception:
                continue

        id_to_name: Dict[str, str] = {}
        id_to_path: Dict[str, str] = {}
        name_to_path: Dict[str, str] = {}

        def traverse_tree(node: Dict[str, Any], parent_path: str = ""):
            gid, gname = extract_gid_and_name(node)
            if gid and gname:
                id_to_name[gid] = gname
            current_path = f"{parent_path}/{gname}" if parent_path and gname else (parent_path or gname)
            if gid and current_path:
                id_to_path[gid] = current_path
            if gname and current_path and gname not in name_to_path:
                name_to_path[gname] = current_path

            for child in extract_children(node):
                if isinstance(child, dict):
                    traverse_tree(child, current_path)

        for root_node in all_group_roots:
            if isinstance(root_node, dict):
                traverse_tree(root_node, "")

        # 扁平结构关联构建
        nodes_by_id: Dict[str, Dict[str, Any]] = {}
        children_by_parent: Dict[str, List[str]] = {}
        flat_root_ids: List[str] = []

        for item in all_group_roots:
            if not isinstance(item, dict):
                continue
            gid, gname = extract_gid_and_name(item)
            if not gid:
                continue
            nodes_by_id[gid] = item
            if gname:
                id_to_name[gid] = gname
            pid = extract_pid(item)
            if pid not in children_by_parent:
                children_by_parent[pid] = []
            children_by_parent[pid].append(gid)

        for gid, item in nodes_by_id.items():
            pid = extract_pid(item)
            if pid == "0" or pid == "None" or pid == "" or pid not in nodes_by_id:
                flat_root_ids.append(gid)

        def build_flat_paths(curr_id: str, parent_path: str):
            node = nodes_by_id.get(curr_id)
            if not node:
                return
            _, gname = extract_gid_and_name(node)
            current_path = f"{parent_path}/{gname}" if parent_path and gname else (parent_path or gname)
            if current_path:
                id_to_path[curr_id] = current_path
                if gname and gname not in name_to_path:
                    name_to_path[gname] = current_path
            for cid in children_by_parent.get(curr_id, []):
                build_flat_paths(cid, current_path)

        for rid in flat_root_ids:
            build_flat_paths(rid, "")

        return id_to_name, id_to_path, name_to_path
    def resolve_group_path(
        self,
        item: Dict[str, Any],
        id_to_name: Dict[str, str],
        id_to_path: Dict[str, str],
        name_to_path: Dict[str, str],
    ) -> Optional[str]:
        """从 item 解析出语义化多级分组路径（如 '系统管理/用户管理/租户管理'）"""
        raw_path = item.get("group_path") or item.get("group_name_path") or item.get("groupPath")
        leaf_name = str(item.get("group_name") or item.get("groupName") or "").strip()

        # 1. 如果 group_path 是类似 "1906207,1906234,1906268" 的 ID 路径或数组
        if raw_path is not None:
            if isinstance(raw_path, str):
                segments = [s.strip() for s in raw_path.replace("\\", "/").split(",") if s.strip()]
                # 如果不含逗号且包含斜杠，可能是已经命名的路径
                if len(segments) == 1 and "/" in raw_path:
                    segments = [s.strip() for s in raw_path.split("/") if s.strip()]
            elif isinstance(raw_path, (list, tuple)):
                segments = [str(s).strip() for s in raw_path if str(s).strip()]
            else:
                segments = [str(raw_path).strip()]

            if segments:
                # 判断 segments 是否主要是数字 ID
                is_all_ids = all(s.isdigit() for s in segments)
                if is_all_ids:
                    resolved_names: List[str] = []
                    for idx, gid in enumerate(segments):
                        if gid in id_to_name:
                            resolved_names.append(id_to_name[gid])
                        elif idx == len(segments) - 1 and leaf_name:
                            resolved_names.append(leaf_name)
                        # 如果无法翻译该数字 ID，则跳过纯数字 ID 避免展示数字
                    if resolved_names:
                        return "/".join(resolved_names)
                else:
                    # 已经是名称数组
                    return "/".join(segments)

        # 2. 如果有 group_id，查 id_to_path
        gid = item.get("group_id") if item.get("group_id") is not None else (
            item.get("groupID") if item.get("groupID") is not None else item.get("api_group_id")
        )
        if gid is not None and str(gid) in id_to_path:
            return id_to_path[str(gid)]

        # 3. 如果有 group_name，查 name_to_path
        if leaf_name:
            if leaf_name in name_to_path:
                return name_to_path[leaf_name]
            return leaf_name

        return None

    def list_apis(self, project_id: str, page: int = 1, size: int = 100) -> List[Dict[str, Any]]:
        fetch_size = max(size, 1000)
        curr_page = page
        all_raw_items: List[Dict[str, Any]] = []
        id_to_name, id_to_path, name_to_path = self.get_group_maps(project_id)

        while True:
            query: Dict[str, Any] = {
                "project_id": project_id,
                "project_hash_key": project_id,
                "projectHashKey": project_id,
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
                    # 如果响应内直接携带了 groupList，合并到映射
                    sub_groups = data.get("groupList") or data.get("groups") or data.get("apiGroupList") or []
                    if isinstance(sub_groups, list):
                        for g in sub_groups:
                            if isinstance(g, dict):
                                gid = g.get("group_id") if g.get("group_id") is not None else g.get("groupID")
                                gname = g.get("group_name") or g.get("groupName")
                                if gid is not None and gname:
                                    id_to_name[str(gid)] = str(gname).strip()
                elif isinstance(data, list):
                    page_items = data
            elif isinstance(res, list):
                page_items = res

            if not page_items:
                break

            all_raw_items.extend(page_items)

            if paginator and isinstance(paginator, dict):
                total = paginator.get("total") or 0
                if len(all_raw_items) >= total:
                    break
                curr_page += 1
                if curr_page > 10:
                    break
            else:
                break

        # 第一遍扫描：收集项目内所有接口声明的 group_id -> group_name 对应关系
        for item in all_raw_items:
            if isinstance(item, dict):
                gid = item.get("group_id") if item.get("group_id") is not None else (
                    item.get("groupID") if item.get("groupID") is not None else item.get("api_group_id")
                )
                gname = str(item.get("group_name") or item.get("groupName") or "").strip()
                if gid is not None and gname:
                    id_to_name[str(gid)] = gname

        items = all_raw_items
        result = []
        for item in items:
            raw_id = str(item.get("api_id") or item.get("id") or item.get("apiID") or "")
            item_id = f"{project_id}:{raw_id}" if raw_id else f"{project_id}:unknown"
            name = item.get("api_name") or item.get("name") or item.get("apiName") or "未命名接口"
            protocol, method = resolve_data_item_protocol_and_method(item)
            path = item.get("api_uri") or item.get("path") or item.get("uri") or item.get("apiURI")
            extra = []

            resolved_group = self.resolve_group_path(item, id_to_name, id_to_path, name_to_path)
            if resolved_group:
                extra.append({"label": "分组", "value": str(resolved_group)})

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

    def fetch_api_raw(self, item_id: str) -> Tuple[str, str, Dict[str, Any]]:
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
        return pid, aid, norm_raw

    def get_api_detail(self, item_id: str) -> Dict[str, Any]:
        pid, _aid, norm_raw = self.fetch_api_raw(item_id)

        name = norm_raw.get("api_name") or norm_raw.get("name") or norm_raw.get("apiName") or "未命名接口"
        protocol, method = resolve_data_item_protocol_and_method(norm_raw)
        path = norm_raw.get("api_uri") or norm_raw.get("path") or norm_raw.get("uri") or norm_raw.get("apiURI")

        extra = []
        id_to_name, id_to_path, name_to_path = self.get_group_maps(pid) if pid else ({}, {}, {})
        resolved_group = self.resolve_group_path(norm_raw, id_to_name, id_to_path, name_to_path)
        if resolved_group:
            extra.append({"label": "分组", "value": str(resolved_group)})
        status = norm_raw.get("api_status") if norm_raw.get("api_status") is not None else norm_raw.get("apiStatus")
        if status is not None:
            status_desc = API_STATUS_MAP.get(status, str(status)) if isinstance(status, int) else str(status)
            extra.append({"label": "状态", "value": status_desc})
        raw_proto = norm_raw.get("api_protocol") if norm_raw.get("api_protocol") is not None else norm_raw.get("apiProtocol")
        if raw_proto is not None:
            proto_desc = API_PROTOCOL_MAP.get(raw_proto, str(raw_proto)) if isinstance(raw_proto, int) else str(raw_proto)
            extra.append({"label": "协议", "value": proto_desc})
        update_time = norm_raw.get("api_update_time") or norm_raw.get("apiUpdateTime")
        if update_time:
            extra.append({"label": "更新时间", "value": str(update_time)})

        return {
            "id": item_id,
            "name": name,
            "protocol": protocol,
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
            _pid, _aid, raw = client.fetch_api_raw(item_id)
            template = convert_api_to_template(item_id, raw, base_url_config=client.base_url)
            templates.append(template)
        except Exception as e:
            sys.stderr.write(f"eolink get_templates warning for {item_id}: {e}\n")
    return templates


def handle_validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    client = EolinkClient(config)
    return client.validate_config()


def get_plugin_entry() -> Dict[str, Any]:
    """返回 describe 握手使用的 entry（单一事实来源 = 同级 plugin.json）。

    宿主扫描时用磁盘 plugin.json 的 entry 拉起本进程做 describe，握手后所有
    后续拉起（browse / validate_config / execute）改用 describe 回显的 entry
    （flow-engine-plugin/manager.rs 以回显 manifest 覆盖磁盘 manifest）。
    因此回显必须与磁盘 entry 一致：开发态（python3 ./eolink.py）与打包态
    （./eolink.exe）都由 plugin.json 决定，脚本不感知打包细节。

    PyInstaller onefile 打包后 __file__ 指向临时解压目录，必须按
    sys.executable 定位 plugin.json；源码运行则按 __file__ 定位。
    """
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    manifest_path = os.path.join(base_dir, "plugin.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        entry = manifest.get("entry")
        if isinstance(entry, dict) and entry.get("command"):
            return entry
    except Exception as e:
        sys.stderr.write(f"eolink: cannot read {manifest_path}: {e}\n")
    # 兜底：plugin.json 缺失/损坏时按运行方式推断（frozen → 自身二进制）
    if getattr(sys, "frozen", False):
        binary_name = os.path.basename(sys.executable)
        return {"command": f"./{binary_name}", "args": []}
    return {"command": sys.executable, "args": [os.path.abspath(__file__)]}


def describe_result() -> Dict[str, Any]:
    return {
        "plugin": {
            "name": "eolink",
            "version": "0.1.5",
            "kind": "datasource",
            "description": "Eolink 数据源（只读浏览 + 拖拽导入）",
            "entry": get_plugin_entry(),
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
        entry = desc["plugin"]["entry"]
        assert entry.get("command"), "describe entry.command must be non-empty"
        manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json")
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            disk_entry = manifest.get("entry")
            if isinstance(disk_entry, dict):
                assert entry == disk_entry, (
                    f"describe entry {entry} must match plugin.json entry {disk_entry}"
                )
        print("  [PASS] describe test passed")
    except Exception as e:
        print(f"  [FAIL] describe test failed: {e}")
        passed = False

    # 2. Test validate_config (empty api_key failure)
    try:
        res = handle_validate_config({})
        assert res["valid"] is False
        assert res["message"] is not None
        print("  [PASS] validate_config (empty) test passed")
    except Exception as e:
        print(f"  [FAIL] validate_config test failed: {e}")
        passed = False

    # 3. Test mock data mapping for Apikit v3
    class MockClient(EolinkClient):
        def _request(self, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
            if path in ("/v3/api-management/api-groups", "/v3/api-management/groups"):
                return {
                    "statusCode": "000000",
                    "data": [
                        {
                            "group_id": 1906207,
                            "group_name": "系统管理",
                            "parent_group_id": 0,
                        },
                        {
                            "group_id": 1906234,
                            "group_name": "用户管理",
                            "parent_group_id": 1906207,
                        },
                        {
                            "group_id": 1906268,
                            "group_name": "租户管理（系统管理员）",
                            "parent_group_id": 1906234,
                        },
                    ],
                }
            elif path == "/v3/api-management/projects":
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
                        "paginator": {"page": 1, "size": 20, "total": 2},
                        "items": [
                            {
                                "api_id": 237665,
                                "api_name": "获取租户列表",
                                "api_request_type": 1,  # 1 = GET
                                "api_uri": "http://172.18.65.54:8099/tenants",
                                "group_id": 1906268,
                                "group_name": "租户管理（系统管理员）",
                                "group_path": "1906207,1906234,1906268",
                                "api_status": 3,
                            },
                            {
                                "api_id": 237666,
                                "api_name": "获取用户信息",
                                "api_request_type": 1,
                                "api_uri": "http://172.18.65.54:8099/users/1",
                                "group_id": 1906234,
                                "group_name": "用户管理",
                                "api_status": 0,
                            },
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
        print("  [PASS] browse(project) Apikit v3 test passed")
    except Exception as e:
        print(f"  [FAIL] browse(project) Apikit v3 test failed: {e}")
        passed = False

    # Test browse apis with multi-level ID path resolution
    try:
        mock_client = MockClient({"api_key": "mock_key", "project_id": "proj_1001"})
        apis = mock_client.list_apis("proj_1001")
        assert len(apis) == 2
        assert apis[0]["id"] == "proj_1001:237665"
        assert apis[0]["name"] == "获取租户列表"
        assert apis[0]["protocol"] == "http"
        assert apis[0]["method"] == "GET"
        assert any(
            e["label"] == "分组" and e["value"] == "系统管理/用户管理/租户管理（系统管理员）"
            for e in apis[0]["extra"]
        ), f"Expected full group path, got {apis[0]['extra']}"
        assert any(
            e["label"] == "分组" and e["value"] == "系统管理/用户管理"
            for e in apis[1]["extra"]
        ), f"Expected 2-level group path, got {apis[1]['extra']}"
        print("  [PASS] browse(api) Apikit v3 test passed")
    except Exception as e:
        print(f"  [FAIL] browse(api) Apikit v3 test failed: {e}")
        passed = False

    # Test get_item with multi-level group resolution
    try:
        detail = mock_client.get_api_detail("proj_1001:237665")
        assert detail["name"] == "获取用户信息"
        assert detail["method"] == "POST"
        assert len(detail["extra"]) >= 2
        print("  [PASS] get_item Apikit v3 test passed")
    except Exception as e:
        print(f"  [FAIL] get_item Apikit v3 test failed: {e}")
        passed = False

    # Test get_templates
    try:
        raw = mock_client.get_api_detail("proj_1001:237665")["raw"]
        tpl = convert_api_to_template("proj_1001:237665", raw, "http://api.example.com")
        assert tpl["type_id"] == "http:request"
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


        # Test GET with default param_type=0 and empty request_info keeps body_type='none'
        get_empty_raw = {
            "api_name": "获取候选列表",
            "api_uri": "/api/v1/candidates",
            "api_protocol": 0,
            "api_request_type": 1,  # GET
            "api_request_param_type": 0,  # Eolink default Form-data
            "request_info": [],
        }
        get_tpl = convert_api_to_template("proj_1001:237667", get_empty_raw)
        assert get_tpl["type_id"] == "http:request"
        assert get_tpl["data"]["method"] == "get"
        assert get_tpl["data"]["body_type"] == "none"

        # Test WebSocket (api_protocol=2/3)
        ws_raw = {
            "api_name": "实时日志流",
            "api_uri": "116.62.184.125:2000/ws/logs",
            "api_protocol": 2,
            "api_request_type": 0,
        }
        ws_proto, ws_method = resolve_data_item_protocol_and_method(ws_raw)
        assert ws_proto == "websocket" and ws_method == "WS"
        ws_tpl = convert_api_to_template("proj_1001:237668", ws_raw)
        assert ws_tpl["type_id"] == "ws:connect"
        assert ws_tpl["data"]["type"] == "ws:connect"
        assert ws_tpl["data"]["url"] == "ws://116.62.184.125:2000/ws/logs"

        # Test TCP (api_protocol=4)
        tcp_raw = {
            "api_name": "TCP 报文服务",
            "api_uri": "42.194.220.130:5002",
            "api_protocol": 4,
            "api_request_type": 0,
        }
        tcp_proto, tcp_method = resolve_data_item_protocol_and_method(tcp_raw)
        assert tcp_proto == "tcp" and tcp_method == "TCP"
        tcp_tpl = convert_api_to_template("proj_1001:237669", tcp_raw)
        assert tcp_tpl["type_id"] == "tcp:connect"
        assert tcp_tpl["data"]["type"] == "tcp:connect"
        assert tcp_tpl["data"]["host"] == "42.194.220.130"
        assert tcp_tpl["data"]["port"] == 5002

        # Test UDP (api_protocol=5)
        udp_raw = {
            "api_name": "UDP 广播",
            "api_uri": "42.194.220.130:5003",
            "api_protocol": 5,
            "api_request_type": 0,
            "request_info": [{"param_key": "msg", "param_value": "hello", "param_type": "0"}],
        }
        udp_proto, udp_method = resolve_data_item_protocol_and_method(udp_raw)
        assert udp_proto == "other" and udp_method == "UDP"
        udp_tpl = convert_api_to_template("proj_1001:237670", udp_raw)
        assert udp_tpl["type_id"] == "udp:send_to"
        assert udp_tpl["data"]["type"] == "udp:send_to"
        assert udp_tpl["data"]["target_addr"] == "42.194.220.130"
        assert udp_tpl["data"]["target_port"] == 5003
        assert json.loads(udp_tpl["data"]["data"]) == {"msg": "hello"}

        # Test gRPC (api_protocol=6 + data_structure_list expansion)
        grpc_raw = {
            "api_name": "GetFeature",
            "api_uri": "routeguide.RouteGuide.GetFeature",
            "api_protocol": 6,
            "api_request_type": 0,
            "api_request_param_type": 2,
            "custom_info": {
                "interface_name": "routeguide.RouteGuide",
                "method_name": "GetFeature",
            },
            "request_info": [
                {
                    "param_key": "Point",
                    "param_type": "message",
                    "child_list": [{"structure_id": "1061413"}],
                }
            ],
            "data_structure_list": {
                "1061413": {
                    "structure_id": 1061413,
                    "structure_data": [
                        {"param_key": "latitude", "param_type": "int32", "param_value": "409146138"},
                        {"param_key": "longitude", "param_type": "int32", "param_value": "-746188906"},
                    ],
                }
            },
        }
        grpc_proto, grpc_method = resolve_data_item_protocol_and_method(grpc_raw)
        assert grpc_proto == "other" and grpc_method == "gRPC"
        grpc_tpl = convert_api_to_template("proj_1001:237671", grpc_raw)
        assert grpc_tpl["type_id"] == "grpc:call"
        assert grpc_tpl["data"]["type"] == "grpc:call"
        assert grpc_tpl["data"]["service_name"] == "routeguide.RouteGuide"
        assert grpc_tpl["data"]["method_name"] == "GetFeature"
        assert json.loads(grpc_tpl["data"]["request_json"]) == {
            "latitude": 409146138,
            "longitude": -746188906,
        }
        print("  [PASS] get_templates (convert_api_to_template) Apikit v3 test passed")
    except Exception as e:
        print(f"  [FAIL] get_templates test failed: {e}")
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
