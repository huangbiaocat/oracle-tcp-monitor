import asyncio
import base64
import csv
import http.client
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import ssl
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

try:
    import webview
    WEBVIEW_AVAILABLE = True
except Exception:
    webview = None
    WEBVIEW_AVAILABLE = False

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ASSET_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
APP_VERSION = "1.5.9"
REPO_API = "https://api.github.com/repos/huangbiaocat/oracle-tcp-monitor/releases/latest"
UPDATE_CACHE = {"checked_at": 0.0, "data": None}
DB_PATH = ROOT / "oracle_latency.db"
SETTINGS_PATH = ROOT / "oracle_tcp_settings.json"
EXPORT_DIR = ROOT / "导出"
TARGETS_PATH = ASSET_ROOT / "targets.json"
WEB_PATH = ASSET_ROOT / "web" / "index.html"
HOST, WEB_PORT = "127.0.0.1", int(os.environ.get("TCP_PORT", "8765"))
INTERVAL = max(5, int(os.environ.get("TCP_INTERVAL", "60")))
TIMEOUT = max(1.0, float(os.environ.get("TCP_TIMEOUT", "5")))
DURATION_HOURS = max(0.0, float(os.environ.get("TCP_DURATION_HOURS", "48")))
public_ip_cache = {"value": {}, "checked_at": 0.0}
public_ip_lock = threading.Lock()
local_network_cache = {"value": {}, "checked_at": 0.0}


def load_settings():
    defaults = {"interval": INTERVAL, "timeout": TIMEOUT, "duration_hours": DURATION_HOURS, "network_label": "", "project_id": 1}
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        return {
            "interval": max(5, min(86400, int(saved.get("interval", defaults["interval"])))),
            "timeout": max(0.1, min(120.0, float(saved.get("timeout", defaults["timeout"])))),
            "duration_hours": max(0.0, min(8760.0, float(saved.get("duration_hours", defaults["duration_hours"])))),
            "network_label": str(saved.get("network_label", "")).strip()[:80],
            "project_id": max(1, int(saved.get("project_id", 1)))
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return defaults


INITIAL_SETTINGS = load_settings()

stop_event = threading.Event()
wake_event = threading.Event()
round_lock = threading.Lock()
state_lock = threading.Lock()
state = {"running": True, "paused": True, "awaiting_project": True, "started_at": time.time(), "round": 0, "last_round_at": None,
         "last_round_seconds": None, **INITIAL_SETTINGS}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS targets(
          region TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, custom INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS projects(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
          network_label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          detected_isp TEXT, detected_region TEXT, detected_city TEXT, last_public_ipv4 TEXT,
          local_ssid TEXT
        );
        CREATE TABLE IF NOT EXISTS results(
          id INTEGER PRIMARY KEY AUTOINCREMENT, tested_at TEXT NOT NULL, region TEXT NOT NULL,
          latency_ms REAL, success INTEGER NOT NULL, ip TEXT, error TEXT,
          FOREIGN KEY(region) REFERENCES targets(region)
        );
        CREATE INDEX IF NOT EXISTS idx_results_region_time ON results(region, tested_at);
        CREATE INDEX IF NOT EXISTS idx_results_time ON results(tested_at);
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(targets)")}
        if "custom" not in columns:
            conn.execute("ALTER TABLE targets ADD COLUMN custom INTEGER NOT NULL DEFAULT 0")
        if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
            conn.execute("INSERT INTO projects(name,network_label,created_at) VALUES('默认项目','',?)", (utc_now(),))
        project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
        for name in ("detected_isp", "detected_region", "detected_city", "last_public_ipv4", "local_ssid"):
            if name not in project_columns:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {name} TEXT")
        result_columns = {row["name"] for row in conn.execute("PRAGMA table_info(results)")}
        if "project_id" not in result_columns:
            conn.execute("ALTER TABLE results ADD COLUMN project_id INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_results_project_time ON results(project_id,tested_at)")
        targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8-sig"))
        for item in targets:
            region = item["region"]
            host = item.get("host", f"objectstorage.{region}.oraclecloud.com")
            conn.execute("""INSERT INTO targets(region,name,host,port,enabled,custom) VALUES(?,?,?,?,1,0)
              ON CONFLICT(region) DO UPDATE SET name=excluded.name,host=excluded.host,port=excluded.port,custom=0""",
              (region, item["name"], host, int(item.get("port", 443))))


def validate_endpoint(host, port):
    host = str(host or "").strip().strip("[]")
    if not host or len(host) > 253 or any(c.isspace() for c in host) or any(c in host for c in "/?#@"):
        raise ValueError("服务器地址格式无效，请填写域名、IPv4 或 IPv6，不要包含 http:// 或路径")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("服务器域名格式无效")
        if not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", ascii_host):
            raise ValueError("服务器域名格式无效")
        host = ascii_host.lower()
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("端口必须是 1 至 65535 的整数")
    if not 1 <= port <= 65535:
        raise ValueError("端口必须是 1 至 65535 的整数")
    return host, port


def parse_endpoint(value):
    value = str(value or "").strip()
    bracketed = re.fullmatch(r"\[([^]]+)\](?::(\d+))?", value)
    if bracketed:
        return validate_endpoint(bracketed.group(1), bracketed.group(2) or 443)
    if value.count(":") == 1:
        host, candidate = value.rsplit(":", 1)
        if candidate.isdigit():
            return validate_endpoint(host, candidate)
    return validate_endpoint(value, 443)


def list_targets():
    with db() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT region,name,host,port,enabled,custom FROM targets ORDER BY custom,name,region")]
    for row in rows:
        row["enabled"] = bool(row["enabled"])
        row["custom"] = bool(row["custom"])
    return {"targets": rows, "custom_limit": 100}


def list_projects(refresh=False):
    with db() as conn:
        projects = [dict(row) for row in conn.execute("SELECT id,name,network_label,created_at,detected_isp,detected_region,detected_city,last_public_ipv4,local_ssid FROM projects ORDER BY id")]
        result_times = conn.execute("SELECT project_id,tested_at FROM results ORDER BY project_id,tested_at").fetchall()
    with state_lock:
        current = state.get("project_id", 1)
        gap_seconds = max(300, state.get("interval", 60) * 3)
        collecting = not state.get("paused") and state.get("running")
    grouped = {project["id"]: [] for project in projects}
    for row in result_times:
        grouped.setdefault(row["project_id"], []).append(row["tested_at"])
    for project in projects:
        sessions = []
        for stamp in grouped.get(project["id"], []):
            moment = datetime.fromisoformat(stamp)
            if not sessions or (moment - datetime.fromisoformat(sessions[-1]["end"])).total_seconds() > gap_seconds:
                sessions.append({"start": stamp, "end": stamp, "records": 1})
            else:
                sessions[-1]["end"] = stamp
                sessions[-1]["records"] += 1
        project["records"] = sum(item["records"] for item in sessions)
        project["sessions"] = sessions[-100:]
        project["collecting"] = bool(collecting and project["id"] == current)
    current_network = get_public_network(force=refresh)
    current_net = current_network.get("ipv4") or current_network.get("ipv6") or {}
    current_ssid = get_local_network_identity().get("ssid")
    carrier = next((x for x in ("联通", "移动", "电信", "教育网") if x in current_net.get("isp", "")), "")
    region_key = current_net.get("region", "").replace("壮族自治区", "").replace("自治区", "").replace("省", "").replace("市", "")
    city_key = current_net.get("city", "").replace("市", "")
    best_score = 0
    for project in projects:
        score = 0
        if current_ssid and project.get("local_ssid") == current_ssid:
            score += 10
            if project.get("detected_isp") == current_net.get("isp") and current_net.get("isp"):
                score += 3
            if project.get("detected_city") == current_net.get("city") and current_net.get("city"):
                score += 2
        project["match_score"] = score
        best_score = max(best_score, score)
    for project in projects:
        project["recommended"] = bool(best_score >= 10 and project["match_score"] == best_score)
    return {"projects": projects, "current_project_id": current, "current_ssid": current_ssid}


def manage_projects(payload):
    action = payload.get("action")
    if action == "add":
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 80:
            raise ValueError("项目名称必须为 1 至 80 个字符")
        label = str(payload.get("network_label") or name).strip()[:80]
        with db() as conn:
            try:
                cursor = conn.execute("INSERT INTO projects(name,network_label,created_at) VALUES(?,?,?)", (name, label, utc_now()))
            except sqlite3.IntegrityError:
                raise ValueError("同名测试项目已经存在")
            project_id = cursor.lastrowid
        with state_lock:
            state["project_id"], state["network_label"] = project_id, label
        save_current_settings()
    elif action in ("switch", "view", "start"):
        try:
            project_id = int(payload.get("project_id"))
        except (TypeError, ValueError):
            raise ValueError("测试项目无效")
        with db() as conn:
            project = conn.execute("SELECT id,network_label FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("测试项目不存在")
        with state_lock:
            state["project_id"], state["network_label"] = project["id"], project["network_label"]
            if action == "view":
                state["awaiting_project"] = False
                state["paused"] = True
            if action == "start":
                state["awaiting_project"] = False
                state["paused"] = False
                state["running"] = True
                state["started_at"] = time.time()
        if action == "start":
            current_network = get_public_network(force=True)
            detected = current_network.get("ipv4") or current_network.get("ipv6") or {}
            local_ssid = get_local_network_identity().get("ssid")
            with db() as conn:
                conn.execute("UPDATE projects SET detected_isp=?,detected_region=?,detected_city=?,last_public_ipv4=?,local_ssid=COALESCE(local_ssid,?) WHERE id=?",
                             (detected.get("isp"), detected.get("region"), detected.get("city"),
                              (current_network.get("ipv4") or {}).get("ip"), local_ssid, project_id))
        save_current_settings()
    elif action == "update":
        try:
            project_id = int(payload.get("project_id"))
        except (TypeError, ValueError):
            raise ValueError("测试项目无效")
        name = str(payload.get("name") or "").strip()
        label = str(payload.get("network_label") or "").strip()[:80]
        if not name or len(name) > 80:
            raise ValueError("项目名称必须为 1 至 80 个字符")
        with db() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ValueError("测试项目不存在")
            try:
                conn.execute("UPDATE projects SET name=?,network_label=? WHERE id=?", (name, label, project_id))
            except sqlite3.IntegrityError:
                raise ValueError("同名测试项目已经存在")
        with state_lock:
            if state.get("project_id") == project_id:
                state["network_label"] = label
        save_current_settings()
    elif action == "delete":
        try:
            project_id = int(payload.get("project_id"))
        except (TypeError, ValueError):
            raise ValueError("测试项目无效")
        with round_lock:
            with db() as conn:
                if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] <= 1:
                    raise ValueError("至少需要保留一个测试项目")
                if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                    raise ValueError("测试项目不存在")
                conn.execute("DELETE FROM results WHERE project_id=?", (project_id,))
                conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
                fallback = conn.execute("SELECT id,network_label FROM projects ORDER BY id LIMIT 1").fetchone()
        with state_lock:
            if state.get("project_id") == project_id:
                state["project_id"], state["network_label"] = fallback["id"], fallback["network_label"]
        save_current_settings()
    else:
        raise ValueError("不支持的项目操作")
    wake_event.set()
    return list_projects()


def export_project(project_id, download=False):
    """把指定项目连同全部检测记录导出为可移植 JSON 文件，便于复制到其他电脑导入对比。"""
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        raise ValueError("测试项目无效")
    with db() as conn:
        project = conn.execute(
            "SELECT id,name,network_label,created_at,detected_isp,detected_region,detected_city,last_public_ipv4,local_ssid "
            "FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("测试项目不存在")
        rows = [dict(r) for r in conn.execute(
            "SELECT tested_at,region,latency_ms,success,ip,error FROM results WHERE project_id=? ORDER BY tested_at",
            (project_id,))]
    fields = ("name", "network_label", "created_at", "detected_isp", "detected_region",
              "detected_city", "last_public_ipv4", "local_ssid")
    payload = {
        "format": "oracle-tcp-project",
        "version": 1,
        "exported_at": utc_now(),
        "project": {k: project[k] for k in fields},
        "results": rows,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M")
    filename = f"{safe_filename(project['name'])}_{stamp}.json"
    if download:
        return filename, text
    EXPORT_DIR.mkdir(exist_ok=True)
    path = EXPORT_DIR / filename
    path.write_text(text, encoding="utf-8")
    return {"saved": True, "path": str(path), "records": len(rows), "filename": filename}


def list_import_files():
    """列出程序目录中可导入的项目导出文件。"""
    files = []
    for folder in (EXPORT_DIR, ROOT):
        if not folder.exists():
            continue
        for f in sorted(folder.glob("*.json")):
            if f.name == "oracle_tcp_settings.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("format") != "oracle-tcp-project":
                continue
            files.append({
                "path": str(f),
                "name": f.name,
                "size": f.stat().st_size,
                "project_name": (data.get("project") or {}).get("name") or "未知",
                "records": len(data.get("results") or []),
                "exported_at": data.get("exported_at") or "",
            })
    return {"files": files}


def import_project_file(file_path, mode="new"):
    """把导出的项目 JSON 导入本机数据库，mode=new 新建项目，mode=merge 合并到同名项目。"""
    path = Path(str(file_path or ""))
    if not path.is_file():
        raise ValueError("导入文件不存在或无法读取")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise ValueError("文件不是有效的 JSON 导出文件")
    if not isinstance(data, dict) or data.get("format") != "oracle-tcp-project":
        raise ValueError("文件不是 Oracle TCP Monitor 的项目导出文件")
    project = data.get("project") or {}
    name = str(project.get("name") or "").strip()[:80] or "导入项目"
    label = str(project.get("network_label") or "").strip()[:80]
    results = data.get("results") or []
    mode = str(mode or "new").strip().lower()
    if mode not in ("new", "merge"):
        mode = "new"
    with db() as conn:
        if mode == "merge":
            existing = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
            if not existing:
                raise ValueError(f"没有找到同名项目“{name}”，无法合并；请改用“新建项目”导入")
            project_id = existing["id"]
            existing_keys = {row[0] for row in conn.execute(
                "SELECT tested_at || '|' || region FROM results WHERE project_id=?", (project_id,))}
            new_rows = []
            for item in results:
                key = f"{item.get('tested_at')}|{item.get('region')}"
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                new_rows.append((item.get("tested_at"), item.get("region"), item.get("latency_ms"),
                                 1 if item.get("success") else 0, item.get("ip"), item.get("error")))
            conn.executemany(
                "INSERT INTO results(tested_at,region,latency_ms,success,ip,error,project_id) VALUES(?,?,?,?,?,?,?)",
                [(*row, project_id) for row in new_rows])
            added = len(new_rows)
        else:
            base_name = name
            index = 2
            while conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
                name = f"{base_name}（副本{index}）"
                index += 1
            cursor = conn.execute(
                "INSERT INTO projects(name,network_label,created_at,detected_isp,detected_region,detected_city,last_public_ipv4,local_ssid) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (name, label, project.get("created_at") or utc_now(), project.get("detected_isp"),
                 project.get("detected_region"), project.get("detected_city"), project.get("last_public_ipv4"),
                 project.get("local_ssid")))
            project_id = cursor.lastrowid
            rows = [(item.get("tested_at"), item.get("region"), item.get("latency_ms"),
                     1 if item.get("success") else 0, item.get("ip"), item.get("error"), project_id)
                    for item in results]
            conn.executemany(
                "INSERT INTO results(tested_at,region,latency_ms,success,ip,error,project_id) VALUES(?,?,?,?,?,?,?)",
                rows)
            added = len(rows)
    wake_event.set()
    return {"imported": True, "project_id": project_id, "project_name": name, "added": added, **list_projects()}


def save_current_settings():
    with state_lock:
        settings = {key: state[key] for key in ("interval", "timeout", "duration_hours", "network_label", "project_id")}
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def add_custom_target(conn, name, host, port):
    host, port = validate_endpoint(host, port)
    name = str(name or "").strip() or host
    if len(name) > 80:
        raise ValueError("节点名称不能超过 80 个字符")
    if conn.execute("SELECT 1 FROM targets WHERE custom=1 AND lower(host)=lower(?) AND port=?", (host, port)).fetchone():
        raise ValueError(f"自定义节点已存在：{host}:{port}")
    if conn.execute("SELECT COUNT(*) FROM targets WHERE custom=1").fetchone()[0] >= 100:
        raise ValueError("自定义节点最多 100 个")
    region = "custom-" + uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO targets(region,name,host,port,enabled,custom) VALUES(?,?,?,?,1,1)",
                 (region, name, host, port))
    return region


def manage_targets(payload):
    action = payload.get("action")
    with round_lock:
        with db() as conn:
            if action == "add":
                region = add_custom_target(conn, payload.get("name"), payload.get("host"), payload.get("port", 443))
                result = {"added": 1, "region": region}
            elif action == "bulk_add":
                lines = payload.get("lines")
                if not isinstance(lines, list) or not 1 <= len(lines) <= 50:
                    raise ValueError("每次批量导入 1 至 50 行")
                parsed = []
                for raw in lines:
                    text = str(raw or "").strip()
                    if not text:
                        continue
                    parts = re.split(r"[\t,，]", text, maxsplit=1)
                    name, endpoint = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (text, text)
                    host, port = parse_endpoint(endpoint)
                    parsed.append((name, host, port))
                if not parsed:
                    raise ValueError("没有可导入的节点")
                if conn.execute("SELECT COUNT(*) FROM targets WHERE custom=1").fetchone()[0] + len(parsed) > 100:
                    raise ValueError("导入后自定义节点将超过 100 个")
                for name, host, port in parsed:
                    add_custom_target(conn, name, host, port)
                result = {"added": len(parsed)}
            elif action == "update":
                region = str(payload.get("region") or "")
                current = conn.execute("SELECT * FROM targets WHERE region=?", (region,)).fetchone()
                if not current or not current["custom"]:
                    raise ValueError("只能编辑自定义节点")
                host, port = validate_endpoint(payload.get("host"), payload.get("port"))
                name = str(payload.get("name") or "").strip() or host
                if len(name) > 80:
                    raise ValueError("节点名称不能超过 80 个字符")
                duplicate = conn.execute("SELECT 1 FROM targets WHERE custom=1 AND region<>? AND lower(host)=lower(?) AND port=?",
                                         (region, host, port)).fetchone()
                if duplicate:
                    raise ValueError(f"自定义节点已存在：{host}:{port}")
                conn.execute("UPDATE targets SET name=?,host=?,port=? WHERE region=?", (name, host, port, region))
                result = {"updated": region}
            elif action == "toggle":
                region = str(payload.get("region") or "")
                enabled = 1 if payload.get("enabled") else 0
                if not conn.execute("SELECT 1 FROM targets WHERE region=?", (region,)).fetchone():
                    raise ValueError("节点不存在")
                conn.execute("UPDATE targets SET enabled=? WHERE region=?", (enabled, region))
                result = {"updated": region, "enabled": bool(enabled)}
            elif action == "delete":
                region = str(payload.get("region") or "")
                current = conn.execute("SELECT custom FROM targets WHERE region=?", (region,)).fetchone()
                if not current or not current["custom"]:
                    raise ValueError("甲骨文默认节点不能删除，只能停用")
                conn.execute("DELETE FROM results WHERE region=?", (region,))
                conn.execute("DELETE FROM targets WHERE region=?", (region,))
                result = {"deleted": region}
            else:
                raise ValueError("不支持的节点操作")
    wake_event.set()
    result.update(list_targets())
    return result


async def probe(target):
    started = time.perf_counter()
    writer = None
    try:
        with state_lock:
            timeout = state["timeout"]
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target["host"], target["port"]), timeout=timeout)
        latency = round((time.perf_counter() - started) * 1000, 2)
        ip = writer.get_extra_info("peername")[0] if writer.get_extra_info("peername") else None
        return (utc_now(), target["region"], latency, 1, ip, None)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}".strip()[:300]
        return (utc_now(), target["region"], None, 0, None, msg)
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def run_round():
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        targets = [dict(x) for x in conn.execute("SELECT * FROM targets WHERE enabled=1 ORDER BY region")]
    results = await asyncio.gather(*(probe(t) for t in targets))
    with db() as conn:
        conn.executemany("INSERT INTO results(tested_at,region,latency_ms,success,ip,error,project_id) VALUES(?,?,?,?,?,?,?)",
                         [(*result, project_id) for result in results])
    return len(results)


def monitor_loop():
    while not stop_event.is_set():
        with state_lock:
            paused = state["paused"]
            duration_hours = state["duration_hours"]
            deadline = state["started_at"] + duration_hours * 3600 if duration_hours else None
        if paused:
            wake_event.wait(1)
            wake_event.clear()
            continue
        if deadline is not None and time.time() >= deadline:
            with state_lock:
                state["running"] = False
            wake_event.wait(1)
            wake_event.clear()
            continue
        tick = time.perf_counter()
        try:
            with round_lock:
                count = asyncio.run(run_round())
            with state_lock:
                state["round"] += 1
                state["last_round_at"] = utc_now()
                state["last_round_seconds"] = round(time.perf_counter() - tick, 2)
                state["target_count"] = count
                state.pop("error", None)
        except Exception as exc:
            with state_lock:
                state["error"] = f"{type(exc).__name__}: {exc}"
        with state_lock:
            interval = state["interval"]
        wait_for = max(0.2, interval - (time.perf_counter() - tick))
        wake_event.wait(wait_for)
        wake_event.clear()
    with state_lock:
        state["running"] = False


def control(action):
    if action == "pause":
        with state_lock:
            state["paused"] = True
        wake_event.set()
    elif action == "resume":
        with state_lock:
            if state.get("awaiting_project"):
                raise ValueError("请先选择测试项目")
            state["paused"] = False
            state["running"] = True
            duration_hours = state["duration_hours"]
            if duration_hours and time.time() >= state["started_at"] + duration_hours * 3600:
                state["started_at"] = time.time()
        wake_event.set()
    elif action == "restart":
        with state_lock:
            project_id = state.get("project_id", 1)
        with round_lock:
            with db() as conn:
                conn.execute("DELETE FROM results WHERE project_id=?", (project_id,))
            with state_lock:
                state.update({"running": True, "paused": False, "started_at": time.time(), "round": 0,
                              "last_round_at": None, "last_round_seconds": None})
                state.pop("error", None)
        wake_event.set()
    else:
        raise ValueError("unsupported action")
    with state_lock:
        return dict(state)


def update_settings(payload):
    try:
        interval = int(payload.get("interval"))
        timeout = float(payload.get("timeout"))
        duration_hours = float(payload.get("duration_hours"))
    except (TypeError, ValueError):
        raise ValueError("参数必须是数字")
    if not 5 <= interval <= 86400:
        raise ValueError("检测间隔必须在 5 至 86400 秒之间")
    if not 0.1 <= timeout <= 120:
        raise ValueError("连接超时必须在 0.1 至 120 秒之间")
    if not 0 <= duration_hours <= 8760:
        raise ValueError("采集时长必须在 0 至 8760 小时之间；0 表示不限时")
    network_label = str(payload.get("network_label", "")).strip()[:80]
    settings = {"interval": interval, "timeout": timeout, "duration_hours": duration_hours, "network_label": network_label}
    with state_lock:
        was_finished = not state["running"]
        state.update(settings)
        if not state["paused"]:
            state["running"] = True
            if was_finished:
                state["started_at"] = time.time()
        project_id = state.get("project_id", 1)
    with db() as conn:
        conn.execute("UPDATE projects SET network_label=? WHERE id=?", (network_label, project_id))
    save_current_settings()
    wake_event.set()
    with state_lock:
        return dict(state)


def version_tuple(text):
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(x) for x in parts[:3]) or (0,)


def check_update(force=False):
    now = time.time()
    if not force and now - UPDATE_CACHE["checked_at"] < 1800 and UPDATE_CACHE["data"]:
        return UPDATE_CACHE["data"]
    result = {"current_version": APP_VERSION, "frozen": bool(FROZEN), "checked_at": utc_now(),
              "updatable": False, "error": None}
    if not FROZEN:
        result["error"] = "当前是源码运行模式，不支持自动更新；请使用 Releases 的单文件 EXE 版本"
        UPDATE_CACHE.update(checked_at=now, data=result)
        return result
    try:
        request = urllib.request.Request(REPO_API, headers={
            "User-Agent": f"OracleTCPMonitor/{APP_VERSION}",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read(131072).decode("utf-8"))
        tag = str(data.get("tag_name") or "").strip()
        latest = tag.lstrip("vV")
        asset = next((a for a in data.get("assets", []) if str(a.get("name", "")).lower().endswith(".exe")), None)
        result.update({
            "latest_version": latest,
            "latest_tag": tag,
            "release_url": data.get("html_url") or "https://github.com/huangbiaocat/oracle-tcp-monitor/releases/latest",
            "release_notes": (data.get("body") or "").strip()[:4000],
            "asset_name": asset.get("name") if asset else None,
            "asset_url": asset.get("browser_download_url") if asset else None,
            "asset_size": asset.get("size") if asset else None,
        })
        if not asset:
            result["error"] = "最新版本没有找到可下载的 EXE 文件"
        elif version_tuple(latest) <= version_tuple(APP_VERSION):
            result["error"] = f"已是最新版本 v{APP_VERSION}"
        else:
            result["updatable"] = True
    except Exception as exc:
        result["error"] = f"检查更新失败：{type(exc).__name__}: {exc}"
    UPDATE_CACHE.update(checked_at=now, data=result)
    return result


def spawn_updater(new_file, target):
    log_file = new_file.with_name(new_file.name + ".log")
    script_file = new_file.with_name(new_file.name + ".ps1")
    if log_file.exists():
        try:
            log_file.unlink()
        except OSError:
            pass
    q = lambda s: str(s).replace(chr(39), chr(39) + chr(39))
    env_lines = "".join(
        f"$env:{k}='{q(v)}';"
        for k, v in os.environ.items()
        if k.startswith("TCP_")
    )
    inner = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$log='{q(log_file)}';"
        f"$self={os.getpid()};"
        f"$new='{q(new_file)}';"
        f"$old='{q(target)}';"
        "Out-File -FilePath $log -InputObject 'start' -Encoding utf8;"
        "Wait-Process -Id $self;"
        "Out-File -FilePath $log -Append -InputObject 'app-exited' -Encoding utf8;"
        "Start-Sleep -Milliseconds 800;"
        "Out-File -FilePath $log -Append -InputObject 'moving' -Encoding utf8;"
        "$n=0;"
        "while($n -lt 15){ try { Move-Item -LiteralPath $new -Destination $old -Force; break } "
        "catch { Start-Sleep -Milliseconds 500; $n++ } };"
        "Out-File -FilePath $log -Append -InputObject ('moved n=' + $n) -Encoding utf8;"
        "if(Test-Path -LiteralPath $old){ Out-File -FilePath $log -Append -InputObject 'starting' -Encoding utf8; "
        + env_lines +
        "Out-File -FilePath $log -Append -InputObject ('port=' + $env:TCP_PORT) -Encoding utf8; "
        "$p = Start-Process -FilePath $old -PassThru; "
        "Out-File -FilePath $log -Append -InputObject ('started pid=' + $p.Id) -Encoding utf8 };"
        "schtasks /Delete /TN OracleTCPMonitorUpdater /F | Out-Null;"
        f"Remove-Item -LiteralPath '{q(script_file)}' -Force"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    inner_encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")

    def updater_started():
        for _ in range(12):
            if log_file.exists():
                return True
            time.sleep(0.25)
        return False

    # 主方案：通过 WMI 创建更新进程，脱离本程序进程树，新 EXE 可正常启动。
    outer = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Remove-Item Env:_MEIPASS -ErrorAction SilentlyContinue;"
        "Remove-Item Env:_MEIPASS2 -ErrorAction SilentlyContinue;"
        f"$cmd='powershell.exe -NoProfile -WindowStyle Hidden -EncodedCommand {inner_encoded}';"
        "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd } | Out-Null"
    )
    outer_encoded = base64.b64encode(outer.encode("utf-16-le")).decode("ascii")
    clean_env = {k: v for k, v in os.environ.items() if k not in ("_MEIPASS", "_MEIPASS2")}
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", outer_encoded],
        env=clean_env,
        creationflags=flags,
    )
    if updater_started():
        return
    # 备用：任务计划程序。注意 schtasks /TR 有 261 字符限制，因此引用 .ps1 文件。
    try:
        script_file.write_text(inner, encoding="utf-8-sig")
        task_name = "OracleTCPMonitorUpdater"
        task_command = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_file}"'
        created = subprocess.run(
            ["schtasks.exe", "/Create", "/TN", task_name, "/TR", task_command,
             "/SC", "ONCE", "/ST", "23:59", "/F"],
            creationflags=flags, capture_output=True, timeout=15,
        )
        launched = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", task_name],
            creationflags=flags, capture_output=True, timeout=15,
        )
        if created.returncode == 0 and launched.returncode == 0 and updater_started():
            return
    except Exception:
        pass


def apply_update():
    info = check_update(force=True)
    if not info.get("updatable"):
        return info
    target = Path(sys.executable).resolve()
    new_file = target.with_name(target.name + ".update")
    tmp_file = new_file.with_suffix(".download")
    info.update({"downloaded": False, "error": None})
    try:
        request = urllib.request.Request(info["asset_url"], headers={"User-Agent": f"OracleTCPMonitor/{APP_VERSION}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            with open(tmp_file, "wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 256)
        size = tmp_file.stat().st_size
        expected = info.get("asset_size")
        if expected and abs(size - int(expected)) > 64:
            raise ValueError(f"下载文件大小校验失败（{size} 字节，预期 {expected} 字节）")
        os.replace(tmp_file, new_file)
        info.update({
            "downloaded": True,
            "update_file": str(new_file),
            "app_exit_hint": "新版本已下载完成，程序即将退出并自动替换，替换完成后会自动重新打开。",
        })
        spawn_updater(new_file, target)
    except Exception as exc:
        if tmp_file.exists():
            tmp_file.unlink()
        info.update({"updatable": False, "error": f"更新失败：{type(exc).__name__}: {exc}"})
    return info


def cleanup_update_files():
    if not FROZEN:
        return
    target = Path(sys.executable).resolve()
    for suffix in (".update", ".download"):
        path = target.with_name(target.name + suffix)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


REGION_ZH = {"Beijing":"北京市","Tianjin":"天津市","Hebei":"河北省","Shanxi":"山西省","Inner Mongolia":"内蒙古自治区","Liaoning":"辽宁省","Jilin":"吉林省","Heilongjiang":"黑龙江省","Shanghai":"上海市","Jiangsu":"江苏省","Zhejiang":"浙江省","Anhui":"安徽省","Fujian":"福建省","Jiangxi":"江西省","Shandong":"山东省","Henan":"河南省","Hubei":"湖北省","Hunan":"湖南省","Guangdong":"广东省","Guangxi":"广西壮族自治区","Hainan":"海南省","Chongqing":"重庆市","Sichuan":"四川省","Guizhou":"贵州省","Yunnan":"云南省","Tibet":"西藏自治区","Shaanxi":"陕西省","Gansu":"甘肃省","Qinghai":"青海省","Ningxia":"宁夏回族自治区","Xinjiang":"新疆维吾尔自治区","Hong Kong":"香港特别行政区","Macao":"澳门特别行政区","Taiwan":"台湾省"}
CITY_ZH = {"Dongguan":"东莞市","Guangzhou":"广州市","Shenzhen":"深圳市","Nanning":"南宁市","Guilin":"桂林市","Liuzhou":"柳州市","Chongzuo":"崇左市","Qinzhou":"钦州市","Beihai":"北海市","Fangchenggang":"防城港市","Guigang":"贵港市","Yulin":"玉林市","Baise":"百色市","Hezhou":"贺州市","Hechi":"河池市","Laibin":"来宾市","Wuzhou":"梧州市","Beijing":"北京市","Shanghai":"上海市","Chengdu":"成都市","Chongqing":"重庆市","Wuhan":"武汉市","Changsha":"长沙市","Hangzhou":"杭州市","Nanjing":"南京市","Fuzhou":"福州市","Xiamen":"厦门市"}


def direct_json(host, path, family):
    context = ssl.create_default_context()
    last_error = None
    for info in socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM):
        raw = socket.socket(info[0], info[1], info[2])
        raw.settimeout(4)
        try:
            raw.connect(info[4])
            tls = context.wrap_socket(raw, server_hostname=host)
            tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: OracleTCPMonitor/1.2\r\nConnection: close\r\n\r\n".encode("ascii"))
            response = http.client.HTTPResponse(tls)
            response.begin()
            if response.status != 200:
                raise OSError(f"HTTP {response.status}")
            return json.loads(response.read(65536).decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        finally:
            raw.close()
    raise OSError(str(last_error or "network unavailable"))


def chinese_network(data, version):
    public_ip = str(ipaddress.ip_address(data.get("ip")))
    connection = data.get("connection") or {}
    isp_raw = connection.get("isp") or connection.get("org") or data.get("org") or ""
    isp_lower = isp_raw.lower()
    if "unicom" in isp_lower or "china169" in isp_lower:
        isp = "中国联通"
    elif "telecom" in isp_lower or "chinanet" in isp_lower:
        isp = "中国电信"
    elif "mobile" in isp_lower or "cmnet" in isp_lower or "cmi" in isp_lower:
        isp = "中国移动"
    elif "cernet" in isp_lower:
        isp = "中国教育网"
    else:
        isp = isp_raw or "未知运营商"
    country_raw = data.get("country") or data.get("country_name") or ""
    country = "中国" if country_raw in ("China", "CN") else country_raw
    region_raw = (data.get("region") or "").replace(" Sheng", "").replace(" Zhuangzu Zizhiqu", "")
    city_raw = data.get("city") or ""
    region, city = REGION_ZH.get(region_raw, region_raw), CITY_ZH.get(city_raw, city_raw)
    parts = [x for x in (country, region, city, isp, f"IPv{version}: {public_ip}") if x]
    return {"ip":public_ip,"isp":isp,"isp_raw":isp_raw,"asn":connection.get("asn") or data.get("asn"),"city":city,"region":region,"country":country,"display":" · ".join(parts)}


def get_public_network(force=False):
    with public_ip_lock:
        now = time.time()
        local_key = local_identity_key()
        if not force and now - public_ip_cache["checked_at"] < 300 and public_ip_cache.get("local_key") == local_key:
            return public_ip_cache["value"]
        value = {"ipv4": None, "ipv6": None}
        for key, family, version in (("ipv4", socket.AF_INET, 4), ("ipv6", socket.AF_INET6, 6)):
            for host, path in (("ipwho.is", "/?lang=zh"), ("ipapi.co", "/json/")):
                try:
                    data = direct_json(host, path, family)
                    if host == "ipwho.is" and data.get("success") is False:
                        continue
                    value[key] = chinese_network(data, version)
                    break
                except (OSError, ValueError, TypeError):
                    continue
        value["display"] = "\n".join(value[key]["display"] for key in ("ipv4", "ipv6") if value[key])
        public_ip_cache.update(value=value, checked_at=now, local_key=local_key)
        return value


def get_local_network_identity():
    now = time.time()
    if now - local_network_cache["checked_at"] < 30:
        return local_network_cache["value"]
    value = {}
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True,
                                text=True, errors="replace", timeout=4, creationflags=flags)
        match = re.search(r"(?mi)^\s*SSID\s*:\s*(.+?)\s*$", result.stdout)
        if match:
            value["ssid"] = match.group(1).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    local_network_cache.update(value=value, checked_at=now)
    return value


def local_identity_key():
    ssid = get_local_network_identity().get("ssid")
    ip = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    return (ssid, ip)


def network_info(force=False):
    hostname = socket.gethostname()
    ips = set()
    try:
        ips.update(x[4][0] for x in socket.getaddrinfo(hostname, None, socket.AF_INET))
    except OSError:
        pass
    local_outbound_ip = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        local_outbound_ip = sock.getsockname()[0]
        ips.add(local_outbound_ip)
    except OSError:
        pass
    finally:
        sock.close()
    proxy_vars = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if os.environ.get(k)]
    public_network = dict(get_public_network(force=force))
    with state_lock:
        network_label = state.get("network_label", "")
    public_network["custom_label"] = network_label
    if network_label:
        public_network["display"] = network_label + "\n" + public_network.get("display", "")
    public_ip = (public_network.get("ipv4") or public_network.get("ipv6") or {}).get("ip")
    return {"hostname": hostname, "local_ips": sorted(ip for ip in ips if not ip.startswith("127.")),
            "public_ip": public_ip, "outbound_ip": public_ip,
            "public_network": public_network,
            "local_network": get_local_network_identity(),
            "local_outbound_ip": local_outbound_ip, "proxy_environment": bool(proxy_vars),
            "proxy_variables": proxy_vars, "target_service": "Oracle Cloud Object Storage", "target_port": 443}


def percentile(values, p):
    if not values:
        return None
    vals = sorted(values)
    pos = (len(vals) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    return vals[lo] if lo == hi else vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def window_clause(hours):
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = 24.0
    if hours <= 0:
        return 0.0, None
    hours = min(720.0, max(0.1, hours))
    since = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat(timespec="milliseconds")
    return hours, since


def project_window_rows(conn, columns, project_id, since, *, region=None, joins=""):
    """Read either a bounded time window or every retained row for one project."""
    conditions = ["r.project_id=?"]
    params = [project_id]
    if region is not None:
        conditions.insert(0, "r.region=?")
        params.insert(0, region)
    if since is not None:
        conditions.insert(-1, "r.tested_at>=?")
        params.insert(-1, since)
    return conn.execute(
        f"SELECT {columns} FROM results r {joins} WHERE {' AND '.join(conditions)} ORDER BY r.tested_at",
        params,
    ).fetchall()


def summary(hours, p=95):
    try:
        p = min(99, max(50, int(p)))
    except (TypeError, ValueError):
        p = 95
    hours, since = window_clause(hours)
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        targets = [dict(x) for x in conn.execute("SELECT * FROM targets ORDER BY region")]
        rows = project_window_rows(conn, "r.region,r.latency_ms,r.success,r.tested_at,r.ip,r.error", project_id, since)
    grouped = {t["region"]: [] for t in targets}
    for row in rows:
        grouped.setdefault(row["region"], []).append(dict(row))
    output = []
    for t in targets:
        items = grouped[t["region"]]
        oks = [x["latency_ms"] for x in items if x["success"] and x["latency_ms"] is not None]
        last = items[-1] if items else None
        output.append({**t, "samples": len(items), "successes": len(oks),
          "success_rate": round(len(oks) / len(items) * 100, 2) if items else None,
          "avg_ms": round(statistics.fmean(oks), 2) if oks else None,
          "min_ms": round(min(oks), 2) if oks else None,
          "max_ms": round(max(oks), 2) if oks else None,
          "p95_ms": round(percentile(oks, p / 100), 2) if oks else None,
          "jitter_ms": round(statistics.pstdev(oks), 2) if len(oks) > 1 else (0 if oks else None),
          "latest_ms": last["latency_ms"] if last and last["success"] else None,
          "latest_ok": bool(last["success"]) if last else None,
          "latest_at": last["tested_at"] if last else None,
          "ip": last["ip"] if last else None, "error": last["error"] if last else None})
    output.sort(key=lambda x: (x["avg_ms"] is None, x["avg_ms"] or 10**9, -(x["success_rate"] or 0)))
    return {"hours": hours, "project_id": project_id, "p_value": p, "targets": output}


def history(region, hours):
    hours, since = window_clause(hours)
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        rows = project_window_rows(conn, "r.tested_at,r.latency_ms,r.success", project_id, since, region=region)
    # Cap chart payload while preserving the full database.
    step = max(1, math.ceil(len(rows) / 1200))
    return {"region": region, "hours": hours, "points": [dict(x) for x in rows[::step]]}


def csv_bytes(hours):
    selected_hours, since = window_clause(hours)
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        project = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
        rows = project_window_rows(
            conn,
            "r.tested_at,r.region,t.name,t.host,t.port,r.success,r.latency_ms,r.ip,r.error",
            project_id,
            since,
            joins="JOIN targets t ON t.region=r.region",
        )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    info = network_info()
    with state_lock:
        snapshot = dict(state)
    writer.writerow(["Oracle TCP Monitor 导出信息"])
    writer.writerow(["导出时间(本地)", datetime.now().astimezone().isoformat(timespec="seconds")])
    writer.writerow(["测试用途", "当前所在地网络到甲骨文云（Oracle Cloud Infrastructure）各区域服务器的 TCP 连接速度"])
    writer.writerow(["计算机名称", info["hostname"]])
    writer.writerow(["测试项目", project["name"] if project else "默认项目"])
    writer.writerow(["自定义网络标注", info["public_network"].get("custom_label") or "未设置"])
    for key, label in (("ipv4", "IPv4"), ("ipv6", "IPv6")):
        net = info["public_network"].get(key) or {}
        writer.writerow([f"公网{label}", net.get("ip") or "查询失败"])
        writer.writerow([f"{label}运营商", net.get("isp") or "未知"])
        writer.writerow([f"{label}位置", " / ".join(filter(None, (net.get("country"), net.get("region"), net.get("city")))) or "未知"])
        writer.writerow([f"{label} ASN", net.get("asn") or "未知"])
    writer.writerow(["本机局域网出口IP", info["local_outbound_ip"] or "未知"])
    writer.writerow(["本地IPv4", "; ".join(info["local_ips"]) or "未知"])
    writer.writerow(["代理环境变量", "已检测到: " + ", ".join(info["proxy_variables"]) if info["proxy_environment"] else "未检测到"])
    writer.writerow(["目标服务", info["target_service"]])
    writer.writerow(["目标端口", info["target_port"]])
    writer.writerow(["统计窗口", "全部历史" if selected_hours == 0 else f"最近 {selected_hours:g} 小时"])
    writer.writerow(["检测间隔(秒)", snapshot["interval"]])
    writer.writerow(["连接超时(秒)", snapshot["timeout"]])
    writer.writerow(["采集时长(小时)", snapshot["duration_hours"], "0 表示不限时"])
    writer.writerow([])
    writer.writerow(["检测时间(UTC)","区域标识","地区","地址","端口","成功","延迟ms","IP","错误"])
    writer.writerows(rows)
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def safe_filename(text):
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or ""))
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned[:60] or "未命名"


def export_filename(hours):
    """CSV 文件名：项目名称_网络标注_数据起止时间（本机时间）"""
    _, since = window_clause(hours)
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        project = conn.execute("SELECT name,network_label FROM projects WHERE id=?", (project_id,)).fetchone()
        stamps = [row[0] for row in project_window_rows(conn, "r.tested_at", project_id, since)]
    start_stamp = stamps[0] if stamps else (since or utc_now())
    end_stamp = stamps[-1] if stamps else utc_now()

    def local(stamp):
        try:
            return datetime.fromisoformat(stamp).astimezone().strftime("%Y%m%d_%H%M")
        except ValueError:
            return datetime.now().strftime("%Y%m%d_%H%M")

    name = safe_filename(project["name"] if project else "默认项目")
    label = safe_filename(project["network_label"]) if project and project["network_label"] else ""
    parts = [name] + ([label] if label else []) + [f"{local(start_stamp)}-{local(end_stamp)}"]
    return "_".join(parts) + ".csv"


def disposition(filename):
    return f"attachment; filename=\"oracle_tcp_export.csv\"; filename*=UTF-8''{quote(filename)}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_data(self, data, content_type="application/json; charset=utf-8", status=200, headers=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        want_refresh = q.get("refresh", ["0"])[0].lower() in ("1", "true", "yes", "on")
        try:
            if url.path == "/":
                self.send_data(WEB_PATH.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                with state_lock: payload = dict(state)
                payload["version"] = APP_VERSION
                payload["db_size_bytes"] = DB_PATH.stat().st_size if DB_PATH.exists() else 0
                payload["db_path"] = str(DB_PATH)
                try:
                    with db() as conn:
                        payload["db_has_data"] = bool(conn.execute("SELECT 1 FROM results LIMIT 1").fetchone())
                except Exception:
                    payload["db_has_data"] = False
                payload["network"] = network_info(force=want_refresh)
                self.send_data(payload)
            elif url.path == "/api/summary":
                self.send_data(summary(q.get("hours", [24])[0], q.get("p", [95])[0]))
            elif url.path == "/api/history":
                self.send_data(history(q.get("region", [""])[0], q.get("hours", [24])[0]))
            elif url.path == "/api/targets":
                self.send_data(list_targets())
            elif url.path == "/api/projects":
                self.send_data(list_projects(refresh=want_refresh))
            elif url.path == "/api/projects/export":
                if q.get("download", ["0"])[0].lower() in ("1", "true", "yes", "on"):
                    filename, text = export_project(q.get("project_id", [""])[0], download=True)
                    self.send_data(text, "application/json; charset=utf-8",
                                   headers={"Content-Disposition": disposition(filename)})
                else:
                    self.send_data(export_project(q.get("project_id", [""])[0]))
            elif url.path == "/api/projects/import_files":
                self.send_data(list_import_files())
            elif url.path == "/api/update/check":
                self.send_data(check_update(force=want_refresh))
            elif url.path == "/api/export.csv":
                self.send_data(csv_bytes(q.get("hours", [48])[0]), "text/csv; charset=utf-8",
                  headers={"Content-Disposition": disposition(export_filename(q.get("hours", [48])[0]))})
            else:
                self.send_data({"error":"not found"}, status=404)
        except Exception as exc:
            self.send_data({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path not in ("/api/control", "/api/settings", "/api/targets", "/api/projects",
                                "/api/projects/import", "/api/update/apply"):
                self.send_data({"error":"not found"}, status=404)
                return
            length = min(4096, int(self.headers.get("Content-Length", "0")))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/settings":
                self.send_data(update_settings(payload))
            elif url.path == "/api/update/apply":
                self.send_data(apply_update())
            elif url.path == "/api/targets":
                self.send_data(manage_targets(payload))
            elif url.path == "/api/projects":
                self.send_data(manage_projects(payload))
            elif url.path == "/api/projects/import":
                self.send_data(import_project_file(payload.get("file_path"), payload.get("mode", "new")))
            else:
                self.send_data(control(payload.get("action")))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_data({"error": str(exc)}, status=400)
        except Exception as exc:
            self.send_data({"error": f"{type(exc).__name__}: {exc}"}, status=500)


def start_ui(server, url):
    """启动桌面界面：优先原生窗口，其次浏览器，最后无界面。返回 True 表示原生窗口已接管主线程。"""
    ui_mode = os.environ.get("TCP_UI", "auto").strip().lower()
    if os.environ.get("TCP_OPEN_BROWSER", "1") == "0" or ui_mode == "none":
        return False
    if ui_mode == "browser":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        return False
    if WEBVIEW_AVAILABLE:
        try:
            class UiApi:
                def open_browser(self):
                    webbrowser.open(url)
                    return True

                def close_window(self):
                    for window in list(webview.windows):
                        try:
                            window.destroy()
                        except Exception:
                            pass
                    return True

            webview.create_window(
                "Oracle TCP 延迟监控", url,
                width=1320, height=880, min_size=(980, 620),
                js_api=UiApi(), background_color="#08111f",
            )
            webview.start()
            return True
        except Exception as exc:
            print(f"原生窗口启动失败（{type(exc).__name__}: {exc}），改用浏览器打开。")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    return False


def schedule_auto_exit(server, seconds):
    """测试/自动化用：到达指定秒数后自动退出（0 表示不启用）。"""
    if seconds <= 0:
        return

    def _exit():
        time.sleep(seconds)
        stop_event.set()
        try:
            if WEBVIEW_AVAILABLE and webview.windows:
                for window in list(webview.windows):
                    window.destroy()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass

    threading.Thread(target=_exit, daemon=True).start()


def main():
    try:
        cleanup_update_files()
        init_db()
        worker = threading.Thread(target=monitor_loop, name="tcp-monitor", daemon=True)
        worker.start()
        server = ThreadingHTTPServer((HOST, WEB_PORT), Handler)
        url = f"http://{HOST}:{WEB_PORT}"
        print(f"Oracle TCP 延迟监控已启动：{url}")
        print(f"检测间隔 {state['interval']} 秒，TCP 超时 {state['timeout']} 秒，计划时长 {state['duration_hours']:g} 小时")
        server_thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
        server_thread.start()
        schedule_auto_exit(server, float(os.environ.get("TCP_EXIT_AFTER_SECONDS", "0") or 0))
        try:
            if start_ui(server, url):
                stop_event.set()
                server.shutdown()
            else:
                while not stop_event.is_set():
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            server.server_close()
    except Exception:
        try:
            with open(ROOT / "oracle_tcp_error.log", "a", encoding="utf-8") as log:
                log.write(f"\n==== {datetime.now().astimezone().isoformat(timespec='seconds')} ====\n")
                traceback.print_exc(file=log)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
