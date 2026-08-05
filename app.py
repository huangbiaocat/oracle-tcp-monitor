import asyncio
import csv
import http.client
import io
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sqlite3
import statistics
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ASSET_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
DB_PATH = ROOT / "oracle_latency.db"
SETTINGS_PATH = ROOT / "oracle_tcp_settings.json"
TARGETS_PATH = ASSET_ROOT / "targets.json"
WEB_PATH = ASSET_ROOT / "web" / "index.html"
HOST, WEB_PORT = "127.0.0.1", int(os.environ.get("TCP_PORT", "8765"))
INTERVAL = max(5, int(os.environ.get("TCP_INTERVAL", "60")))
TIMEOUT = max(1.0, float(os.environ.get("TCP_TIMEOUT", "5")))
DURATION_HOURS = max(0.0, float(os.environ.get("TCP_DURATION_HOURS", "48")))
public_ip_cache = {"value": {}, "checked_at": 0.0}
public_ip_lock = threading.Lock()


def load_settings():
    defaults = {"interval": INTERVAL, "timeout": TIMEOUT, "duration_hours": DURATION_HOURS, "network_label": "", "project_id": 1}
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
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
state = {"running": True, "paused": False, "started_at": time.time(), "round": 0, "last_round_at": None,
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
          network_label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
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
        conn.execute("INSERT OR IGNORE INTO projects(id,name,network_label,created_at) VALUES(1,'????','',?)", (utc_now(),))
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
        raise ValueError("????????????????IPv4 ? IPv6????? http:// ???")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("?????????")
        if not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", ascii_host):
            raise ValueError("?????????")
        host = ascii_host.lower()
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("????? 1 ? 65535 ???")
    if not 1 <= port <= 65535:
        raise ValueError("????? 1 ? 65535 ???")
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


def list_projects():
    with db() as conn:
        projects = [dict(row) for row in conn.execute("SELECT id,name,network_label,created_at FROM projects ORDER BY id")]
    with state_lock:
        current = state.get("project_id", 1)
    return {"projects": projects, "current_project_id": current}


def manage_projects(payload):
    action = payload.get("action")
    if action == "add":
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 80:
            raise ValueError("??????? 1 ? 80 ???")
        label = str(payload.get("network_label") or name).strip()[:80]
        with db() as conn:
            try:
                cursor = conn.execute("INSERT INTO projects(name,network_label,created_at) VALUES(?,?,?)", (name, label, utc_now()))
            except sqlite3.IntegrityError:
                raise ValueError("??????????")
            project_id = cursor.lastrowid
        with state_lock:
            state["project_id"], state["network_label"] = project_id, label
        save_current_settings()
    elif action == "switch":
        try:
            project_id = int(payload.get("project_id"))
        except (TypeError, ValueError):
            raise ValueError("??????")
        with db() as conn:
            project = conn.execute("SELECT id,network_label FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("???????")
        with state_lock:
            state["project_id"], state["network_label"] = project["id"], project["network_label"]
        save_current_settings()
    else:
        raise ValueError("????????")
    wake_event.set()
    return list_projects()


def save_current_settings():
    with state_lock:
        settings = {key: state[key] for key in ("interval", "timeout", "duration_hours", "network_label", "project_id")}
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def add_custom_target(conn, name, host, port):
    host, port = validate_endpoint(host, port)
    name = str(name or "").strip() or host
    if len(name) > 80:
        raise ValueError("???????? 80 ???")
    if conn.execute("SELECT 1 FROM targets WHERE custom=1 AND lower(host)=lower(?) AND port=?", (host, port)).fetchone():
        raise ValueError(f"?????????{host}:{port}")
    if conn.execute("SELECT COUNT(*) FROM targets WHERE custom=1").fetchone()[0] >= 100:
        raise ValueError("??????? 100 ?")
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
                    raise ValueError("?????? 1 ? 50 ?")
                parsed = []
                for raw in lines:
                    text = str(raw or "").strip()
                    if not text:
                        continue
                    parts = re.split(r"[\t,?]", text, maxsplit=1)
                    name, endpoint = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (text, text)
                    host, port = parse_endpoint(endpoint)
                    parsed.append((name, host, port))
                if not parsed:
                    raise ValueError("????????")
                if conn.execute("SELECT COUNT(*) FROM targets WHERE custom=1").fetchone()[0] + len(parsed) > 100:
                    raise ValueError("??????????? 100 ?")
                for name, host, port in parsed:
                    add_custom_target(conn, name, host, port)
                result = {"added": len(parsed)}
            elif action == "update":
                region = str(payload.get("region") or "")
                current = conn.execute("SELECT * FROM targets WHERE region=?", (region,)).fetchone()
                if not current or not current["custom"]:
                    raise ValueError("?????????")
                host, port = validate_endpoint(payload.get("host"), payload.get("port"))
                name = str(payload.get("name") or "").strip() or host
                if len(name) > 80:
                    raise ValueError("???????? 80 ???")
                duplicate = conn.execute("SELECT 1 FROM targets WHERE custom=1 AND region<>? AND lower(host)=lower(?) AND port=?",
                                         (region, host, port)).fetchone()
                if duplicate:
                    raise ValueError(f"?????????{host}:{port}")
                conn.execute("UPDATE targets SET name=?,host=?,port=? WHERE region=?", (name, host, port, region))
                result = {"updated": region}
            elif action == "toggle":
                region = str(payload.get("region") or "")
                enabled = 1 if payload.get("enabled") else 0
                if not conn.execute("SELECT 1 FROM targets WHERE region=?", (region,)).fetchone():
                    raise ValueError("?????")
                conn.execute("UPDATE targets SET enabled=? WHERE region=?", (enabled, region))
                result = {"updated": region, "enabled": bool(enabled)}
            elif action == "delete":
                region = str(payload.get("region") or "")
                current = conn.execute("SELECT custom FROM targets WHERE region=?", (region,)).fetchone()
                if not current or not current["custom"]:
                    raise ValueError("????????????????")
                conn.execute("DELETE FROM results WHERE region=?", (region,))
                conn.execute("DELETE FROM targets WHERE region=?", (region,))
                result = {"deleted": region}
            else:
                raise ValueError("????????")
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
        raise ValueError("???????")
    if not 5 <= interval <= 86400:
        raise ValueError("??????? 5 ? 86400 ???")
    if not 0.1 <= timeout <= 120:
        raise ValueError("??????? 0.1 ? 120 ???")
    if not 0 <= duration_hours <= 8760:
        raise ValueError("??????? 0 ? 8760 ?????0 ?????")
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


REGION_ZH = {"Beijing":"???","Tianjin":"???","Hebei":"???","Shanxi":"???","Inner Mongolia":"??????","Liaoning":"???","Jilin":"???","Heilongjiang":"????","Shanghai":"???","Jiangsu":"???","Zhejiang":"???","Anhui":"???","Fujian":"???","Jiangxi":"???","Shandong":"???","Henan":"???","Hubei":"???","Hunan":"???","Guangdong":"???","Guangxi":"???????","Hainan":"???","Chongqing":"???","Sichuan":"???","Guizhou":"???","Yunnan":"???","Tibet":"?????","Shaanxi":"???","Gansu":"???","Qinghai":"???","Ningxia":"???????","Xinjiang":"????????","Hong Kong":"???????","Macao":"???????","Taiwan":"???"}
CITY_ZH = {"Dongguan":"???","Guangzhou":"???","Shenzhen":"???","Nanning":"???","Guilin":"???","Liuzhou":"???","Chongzuo":"???","Qinzhou":"???","Beihai":"???","Fangchenggang":"????","Guigang":"???","Yulin":"???","Baise":"???","Hezhou":"???","Hechi":"???","Laibin":"???","Wuzhou":"???","Beijing":"???","Shanghai":"???","Chengdu":"???","Chongqing":"???","Wuhan":"???","Changsha":"???","Hangzhou":"???","Nanjing":"???","Fuzhou":"???","Xiamen":"???"}


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
        isp = "????"
    elif "telecom" in isp_lower or "chinanet" in isp_lower:
        isp = "????"
    elif "mobile" in isp_lower or "cmnet" in isp_lower or "cmi" in isp_lower:
        isp = "????"
    elif "cernet" in isp_lower:
        isp = "?????"
    else:
        isp = isp_raw or "?????"
    country_raw = data.get("country") or data.get("country_name") or ""
    country = "??" if country_raw in ("China", "CN") else country_raw
    region_raw = (data.get("region") or "").replace(" Sheng", "").replace(" Zhuangzu Zizhiqu", "")
    city_raw = data.get("city") or ""
    region, city = REGION_ZH.get(region_raw, region_raw), CITY_ZH.get(city_raw, city_raw)
    parts = [x for x in (country, region, city, isp, f"IPv{version}: {public_ip}") if x]
    return {"ip":public_ip,"isp":isp,"isp_raw":isp_raw,"asn":connection.get("asn") or data.get("asn"),"city":city,"region":region,"country":country,"display":" ? ".join(parts)}


def get_public_network():
    with public_ip_lock:
        now = time.time()
        if now - public_ip_cache["checked_at"] < 300:
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
        public_ip_cache.update(value=value, checked_at=now)
        return value


def network_info():
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
    public_network = dict(get_public_network())
    with state_lock:
        network_label = state.get("network_label", "")
    public_network["custom_label"] = network_label
    if network_label:
        public_network["display"] = network_label + "\n" + public_network.get("display", "")
    public_ip = (public_network.get("ipv4") or public_network.get("ipv6") or {}).get("ip")
    return {"hostname": hostname, "local_ips": sorted(ip for ip in ips if not ip.startswith("127.")),
            "public_ip": public_ip, "outbound_ip": public_ip,
            "public_network": public_network,
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
        hours = min(720.0, max(0.1, float(hours)))
    except (TypeError, ValueError):
        hours = 24.0
    since = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat(timespec="milliseconds")
    return hours, since


def summary(hours):
    hours, since = window_clause(hours)
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        targets = [dict(x) for x in conn.execute("SELECT * FROM targets ORDER BY region")]
        rows = conn.execute("SELECT region,latency_ms,success,tested_at,ip,error FROM results WHERE tested_at>=? AND project_id=? ORDER BY tested_at", (since, project_id)).fetchall()
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
          "p95_ms": round(percentile(oks, .95), 2) if oks else None,
          "jitter_ms": round(statistics.pstdev(oks), 2) if len(oks) > 1 else (0 if oks else None),
          "latest_ms": last["latency_ms"] if last and last["success"] else None,
          "latest_ok": bool(last["success"]) if last else None,
          "latest_at": last["tested_at"] if last else None,
          "ip": last["ip"] if last else None, "error": last["error"] if last else None})
    output.sort(key=lambda x: (x["avg_ms"] is None, x["avg_ms"] or 10**9, -(x["success_rate"] or 0)))
    return {"hours": hours, "project_id": project_id, "targets": output}


def history(region, hours):
    hours, since = window_clause(hours)
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        rows = conn.execute("""SELECT tested_at,latency_ms,success FROM results
          WHERE region=? AND tested_at>=? AND project_id=? ORDER BY tested_at""", (region, since, project_id)).fetchall()
    # Cap chart payload while preserving the full database.
    step = max(1, math.ceil(len(rows) / 1200))
    return {"region": region, "hours": hours, "points": [dict(x) for x in rows[::step]]}


def csv_bytes(hours):
    selected_hours, since = window_clause(hours)
    with state_lock:
        project_id = state.get("project_id", 1)
    with db() as conn:
        project = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
        rows = conn.execute("""SELECT r.tested_at,r.region,t.name,t.host,t.port,r.success,r.latency_ms,r.ip,r.error
          FROM results r JOIN targets t ON t.region=r.region WHERE r.tested_at>=? AND r.project_id=?
          ORDER BY r.tested_at,r.region""", (since, project_id)).fetchall()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    info = network_info()
    with state_lock:
        snapshot = dict(state)
    writer.writerow(["Oracle TCP Monitor ????"])
    writer.writerow(["????(??)", datetime.now().astimezone().isoformat(timespec="seconds")])
    writer.writerow(["????", "?????????????Oracle Cloud Infrastructure???????? TCP ????"])
    writer.writerow(["?????", info["hostname"]])
    writer.writerow(["????", project["name"] if project else "????"])
    writer.writerow(["???????", info["public_network"].get("custom_label") or "???"])
    for key, label in (("ipv4", "IPv4"), ("ipv6", "IPv6")):
        net = info["public_network"].get(key) or {}
        writer.writerow([f"??{label}", net.get("ip") or "????"])
        writer.writerow([f"{label}???", net.get("isp") or "??"])
        writer.writerow([f"{label}??", " / ".join(filter(None, (net.get("country"), net.get("region"), net.get("city")))) or "??"])
        writer.writerow([f"{label} ASN", net.get("asn") or "??"])
    writer.writerow(["???????IP", info["local_outbound_ip"] or "??"])
    writer.writerow(["??IPv4", "; ".join(info["local_ips"]) or "??"])
    writer.writerow(["??????", "????: " + ", ".join(info["proxy_variables"]) if info["proxy_environment"] else "????"])
    writer.writerow(["????", info["target_service"]])
    writer.writerow(["????", info["target_port"]])
    writer.writerow(["????(??)", selected_hours])
    writer.writerow(["????(?)", snapshot["interval"]])
    writer.writerow(["????(?)", snapshot["timeout"]])
    writer.writerow(["????(??)", snapshot["duration_hours"], "0 ?????"])
    writer.writerow([])
    writer.writerow(["????(UTC)","????","??","??","??","??","??ms","IP","??"])
    writer.writerows(rows)
    return ("\ufeff" + out.getvalue()).encode("utf-8")


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
        try:
            if url.path == "/":
                self.send_data(WEB_PATH.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                with state_lock: payload = dict(state)
                payload["db_size_bytes"] = DB_PATH.stat().st_size if DB_PATH.exists() else 0
                payload["network"] = network_info()
                self.send_data(payload)
            elif url.path == "/api/summary":
                self.send_data(summary(q.get("hours", [24])[0]))
            elif url.path == "/api/history":
                self.send_data(history(q.get("region", [""])[0], q.get("hours", [24])[0]))
            elif url.path == "/api/targets":
                self.send_data(list_targets())
            elif url.path == "/api/projects":
                self.send_data(list_projects())
            elif url.path == "/api/export.csv":
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.send_data(csv_bytes(q.get("hours", [48])[0]), "text/csv; charset=utf-8",
                  headers={"Content-Disposition": f'attachment; filename="oracle_tcp_{stamp}.csv"'})
            else:
                self.send_data({"error":"not found"}, status=404)
        except Exception as exc:
            self.send_data({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path not in ("/api/control", "/api/settings", "/api/targets", "/api/projects"):
                self.send_data({"error":"not found"}, status=404)
                return
            length = min(4096, int(self.headers.get("Content-Length", "0")))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/settings":
                self.send_data(update_settings(payload))
            elif url.path == "/api/targets":
                self.send_data(manage_targets(payload))
            elif url.path == "/api/projects":
                self.send_data(manage_projects(payload))
            else:
                self.send_data(control(payload.get("action")))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_data({"error": str(exc)}, status=400)
        except Exception as exc:
            self.send_data({"error": f"{type(exc).__name__}: {exc}"}, status=500)


def main():
    init_db()
    worker = threading.Thread(target=monitor_loop, name="tcp-monitor", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((HOST, WEB_PORT), Handler)
    print(f"Oracle TCP ????????http://{HOST}:{WEB_PORT}")
    print(f"???? {state['interval']} ??TCP ?? {state['timeout']} ?????? {state['duration_hours']:g} ??")
    if os.environ.get("TCP_OPEN_BROWSER", "1") != "0":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{WEB_PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
