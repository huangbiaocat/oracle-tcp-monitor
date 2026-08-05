import asyncio
import csv
import io
import ipaddress
import json
import math
import os
import re
import socket
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


def load_settings():
    defaults = {"interval": INTERVAL, "timeout": TIMEOUT, "duration_hours": DURATION_HOURS}
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return {
            "interval": max(5, min(86400, int(saved.get("interval", defaults["interval"])))),
            "timeout": max(0.1, min(120.0, float(saved.get("timeout", defaults["timeout"])))),
            "duration_hours": max(0.0, min(8760.0, float(saved.get("duration_hours", defaults["duration_hours"]))))
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
    with db() as conn:
        targets = [dict(x) for x in conn.execute("SELECT * FROM targets WHERE enabled=1 ORDER BY region")]
    results = await asyncio.gather(*(probe(t) for t in targets))
    with db() as conn:
        conn.executemany("INSERT INTO results(tested_at,region,latency_ms,success,ip,error) VALUES(?,?,?,?,?,?)", results)
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
        with round_lock:
            with db() as conn:
                conn.execute("DELETE FROM results")
                conn.execute("DELETE FROM sqlite_sequence WHERE name='results'")
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
    settings = {"interval": interval, "timeout": timeout, "duration_hours": duration_hours}
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    with state_lock:
        was_finished = not state["running"]
        state.update(settings)
        if not state["paused"]:
            state["running"] = True
            if was_finished:
                state["started_at"] = time.time()
    wake_event.set()
    with state_lock:
        return dict(state)


def network_info():
    hostname = socket.gethostname()
    ips = set()
    try:
        ips.update(x[4][0] for x in socket.getaddrinfo(hostname, None, socket.AF_INET))
    except OSError:
        pass
    outbound_ip = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        outbound_ip = sock.getsockname()[0]
        ips.add(outbound_ip)
    except OSError:
        pass
    finally:
        sock.close()
    proxy_vars = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if os.environ.get(k)]
    return {"hostname": hostname, "local_ips": sorted(ip for ip in ips if not ip.startswith("127.")),
            "outbound_ip": outbound_ip, "proxy_environment": bool(proxy_vars),
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
    with db() as conn:
        targets = [dict(x) for x in conn.execute("SELECT * FROM targets ORDER BY region")]
        rows = conn.execute("SELECT region,latency_ms,success,tested_at,ip,error FROM results WHERE tested_at>=? ORDER BY tested_at", (since,)).fetchall()
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
    return {"hours": hours, "targets": output}


def history(region, hours):
    hours, since = window_clause(hours)
    with db() as conn:
        rows = conn.execute("""SELECT tested_at,latency_ms,success FROM results
          WHERE region=? AND tested_at>=? ORDER BY tested_at""", (region, since)).fetchall()
    # Cap chart payload while preserving the full database.
    step = max(1, math.ceil(len(rows) / 1200))
    return {"region": region, "hours": hours, "points": [dict(x) for x in rows[::step]]}


def csv_bytes(hours):
    selected_hours, since = window_clause(hours)
    with db() as conn:
        rows = conn.execute("""SELECT r.tested_at,r.region,t.name,t.host,t.port,r.success,r.latency_ms,r.ip,r.error
          FROM results r JOIN targets t ON t.region=r.region WHERE r.tested_at>=?
          ORDER BY r.tested_at,r.region""", (since,)).fetchall()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    info = network_info()
    with state_lock:
        snapshot = dict(state)
    writer.writerow(["Oracle TCP Monitor 导出信息"])
    writer.writerow(["导出时间(本地)", datetime.now().astimezone().isoformat(timespec="seconds")])
    writer.writerow(["测试用途", "当前所在地网络到甲骨文云（Oracle Cloud Infrastructure）各区域服务器的 TCP 连接速度"])
    writer.writerow(["计算机名称", info["hostname"]])
    writer.writerow(["本地出口IP", info["outbound_ip"] or "未知"])
    writer.writerow(["本地IPv4", "; ".join(info["local_ips"]) or "未知"])
    writer.writerow(["代理环境变量", "已检测到: " + ", ".join(info["proxy_variables"]) if info["proxy_environment"] else "未检测到"])
    writer.writerow(["目标服务", info["target_service"]])
    writer.writerow(["目标端口", info["target_port"]])
    writer.writerow(["统计窗口(小时)", selected_hours])
    writer.writerow(["检测间隔(秒)", snapshot["interval"]])
    writer.writerow(["连接超时(秒)", snapshot["timeout"]])
    writer.writerow(["采集时长(小时)", snapshot["duration_hours"], "0 表示不限时"])
    writer.writerow([])
    writer.writerow(["检测时间(UTC)","区域标识","地区","地址","端口","成功","延迟ms","IP","错误"])
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
            if url.path not in ("/api/control", "/api/settings", "/api/targets"):
                self.send_data({"error":"not found"}, status=404)
                return
            length = min(4096, int(self.headers.get("Content-Length", "0")))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/settings":
                self.send_data(update_settings(payload))
            elif url.path == "/api/targets":
                self.send_data(manage_targets(payload))
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
    print(f"Oracle TCP 延迟监控已启动：http://{HOST}:{WEB_PORT}")
    print(f"检测间隔 {state['interval']} 秒，TCP 超时 {state['timeout']} 秒，计划时长 {state['duration_hours']:g} 小时")
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
