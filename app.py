import asyncio
import csv
import io
import json
import math
import os
import socket
import sqlite3
import statistics
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ASSET_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
DB_PATH = ROOT / "oracle_latency.db"
TARGETS_PATH = ASSET_ROOT / "targets.json"
WEB_PATH = ASSET_ROOT / "web" / "index.html"
HOST, WEB_PORT = "127.0.0.1", 8765
INTERVAL = max(5, int(os.environ.get("TCP_INTERVAL", "60")))
TIMEOUT = max(1.0, float(os.environ.get("TCP_TIMEOUT", "5")))
DURATION_HOURS = max(0.0, float(os.environ.get("TCP_DURATION_HOURS", "48")))

stop_event = threading.Event()
wake_event = threading.Event()
round_lock = threading.Lock()
state_lock = threading.Lock()
state = {"running": True, "paused": False, "started_at": time.time(), "round": 0, "last_round_at": None,
         "last_round_seconds": None, "interval": INTERVAL, "timeout": TIMEOUT,
         "duration_hours": DURATION_HOURS}


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
          enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS results(
          id INTEGER PRIMARY KEY AUTOINCREMENT, tested_at TEXT NOT NULL, region TEXT NOT NULL,
          latency_ms REAL, success INTEGER NOT NULL, ip TEXT, error TEXT,
          FOREIGN KEY(region) REFERENCES targets(region)
        );
        CREATE INDEX IF NOT EXISTS idx_results_region_time ON results(region, tested_at);
        CREATE INDEX IF NOT EXISTS idx_results_time ON results(tested_at);
        """)
        targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8-sig"))
        for item in targets:
            region = item["region"]
            host = item.get("host", f"objectstorage.{region}.oraclecloud.com")
            conn.execute("""INSERT INTO targets(region,name,host,port,enabled) VALUES(?,?,?,?,1)
              ON CONFLICT(region) DO UPDATE SET name=excluded.name,host=excluded.host,port=excluded.port""",
              (region, item["name"], host, int(item.get("port", 443))))


async def probe(target):
    started = time.perf_counter()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target["host"], target["port"]), timeout=TIMEOUT)
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
            deadline = state["started_at"] + DURATION_HOURS * 3600 if DURATION_HOURS else None
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
        wait_for = max(0.2, INTERVAL - (time.perf_counter() - tick))
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
            if DURATION_HOURS and time.time() >= state["started_at"] + DURATION_HOURS * 3600:
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
    _, since = window_clause(hours)
    with db() as conn:
        rows = conn.execute("""SELECT r.tested_at,r.region,t.name,t.host,t.port,r.success,r.latency_ms,r.ip,r.error
          FROM results r JOIN targets t ON t.region=r.region WHERE r.tested_at>=?
          ORDER BY r.tested_at,r.region""", (since,)).fetchall()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
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
                self.send_data(payload)
            elif url.path == "/api/summary":
                self.send_data(summary(q.get("hours", [24])[0]))
            elif url.path == "/api/history":
                self.send_data(history(q.get("region", [""])[0], q.get("hours", [24])[0]))
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
            if url.path != "/api/control":
                self.send_data({"error":"not found"}, status=404)
                return
            length = min(4096, int(self.headers.get("Content-Length", "0")))
            payload = json.loads(self.rfile.read(length) or b"{}")
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
    print(f"检测间隔 {INTERVAL} 秒，TCP 超时 {TIMEOUT} 秒，计划时长 {DURATION_HOURS:g} 小时")
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
