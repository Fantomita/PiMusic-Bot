import asyncio
import datetime
import difflib
import json
import logging
import os
import psutil
import random
import re
import sys
import signal
from uuid import uuid4
from dotenv import load_dotenv

import discord
from discord.ext import commands, tasks
from discord import ui, app_commands
import yt_dlp

# --- Import Web Dashboard ---
from quart import Quart, render_template_string, request, jsonify, make_response, redirect, send_from_directory
from pyngrok import ngrok, conf

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot_logs.txt", mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def log_error(msg): logging.error(msg)
def log_info(msg): logging.info(msg)

try:
    from gpiozero import LED
    status_led = LED(17)
    log_info("✅ GPIO LED enabled on Pin 17")
except Exception:
    status_led = None
    log_info("⚠️ GPIO disabled")

sys.dont_write_bytecode = True
try: os.nice(-15)
except: pass

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
NGROK_TOKEN = os.getenv('NGROK_AUTH_TOKEN')

if not TOKEN:
    log_error("❌ ERROR: DISCORD_TOKEN missing.")
    sys.exit(1)

# --- Files & Paths ---
CACHE_DIR = './music_cache'
CACHE_MAP_FILE = 'cache_map.json'
PLAYLIST_FILE = 'playlists.json'
SETTINGS_FILE = 'server_settings.json'
MAX_CACHE_SIZE_GB = 16

if not os.path.exists(CACHE_DIR): os.makedirs(CACHE_DIR)

# ==========================================
# 2. AUDIO SETTINGS
# ==========================================

COLOR_MAIN = 0xFFD700

FFMPEG_STREAM_OPTS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin', 'options': '-vn -threads 2 -bufsize 8192k'}
FFMPEG_LOCAL_OPTS = {'options': '-vn -threads 2 -bufsize 8192k'}

COMMON_YDL_ARGS = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'socket_timeout': 30}

YDL_PLAY_OPTS = {'format': 'bestaudio[ext=webm]/bestaudio/best', **COMMON_YDL_ARGS}
YDL_FLAT_OPTS = {'extract_flat': 'in_playlist', **COMMON_YDL_ARGS}
YDL_MIX_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '1-20', **COMMON_YDL_ARGS, 'noplaylist': False}

# Thumbnails enabled + JPG conversion
YDL_DOWNLOAD_OPTS = {
    'format': 'bestaudio[ext=webm]/bestaudio/best',
    'outtmpl': f'{CACHE_DIR}/%(id)s.%(ext)s',
    'writethumbnail': True,
    'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
    **COMMON_YDL_ARGS
}

YDL_PLAYLIST_LOAD_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '1-50', **COMMON_YDL_ARGS, 'noplaylist': False}

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================

def load_json(filename):
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f: return json.load(f)
        except: return {}
    return {}

def save_json(filename, data):
    with open(filename, 'w') as f: json.dump(data, f)

cache_map = load_json(CACHE_MAP_FILE)
saved_playlists = load_json(PLAYLIST_FILE)
server_settings = load_json(SETTINGS_FILE)

def format_time(seconds):
    if not seconds: return "0:00"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h > 0 else f"{m}:{s:02}"

def is_too_similar(title1, title2):
    def clean(s):
        s = s.lower()
        for w in ["official", "video", "lyrics", "audio", "hq", "hd", "4k", "music", "visualizer", "remix"]:
            s = s.replace(w, "")
        return re.sub(r'\W+', '', s)
    t1, t2 = clean(title1), clean(title2)
    if len(t1) < 3 or len(t2) < 3: return t1 == t2
    return t1 in t2 or t2 in t1

def enforce_cache_limit():
    max_bytes = MAX_CACHE_SIZE_GB * 1024 * 1024 * 1024
    files = []
    total_size = 0
    with os.scandir(CACHE_DIR) as it:
        for entry in it:
            if entry.is_file():
                total_size += entry.stat().st_size
                if entry.name.endswith('.webm'): files.append(entry)
    if total_size > max_bytes:
        files.sort(key=lambda x: x.stat().st_mtime)
        for entry in files:
            try:
                os.remove(entry.path)
                thumb_path = entry.path.replace('.webm', '.jpg')
                if os.path.exists(thumb_path): os.remove(thumb_path)
                total_size -= entry.stat().st_size
                vid_id = entry.name.replace('.webm', '')
                if vid_id in cache_map: del cache_map[vid_id]
                if total_size <= (max_bytes - 100 * 1024 * 1024): break
            except: pass
        save_json(CACHE_MAP_FILE, cache_map)

def get_thumbnail_url(vid_id):
    if os.path.exists(f"{CACHE_DIR}/{vid_id}.jpg"):
        return f"/cache/thumb/{vid_id}.jpg"
    return f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"

# ==========================================
# 4. WEB DASHBOARD
# ==========================================

app = Quart(__name__)
logging.getLogger('quart.serving').setLevel(logging.ERROR)
logging.getLogger('hypercorn.error').setLevel(logging.ERROR)
bot_instance = None 

# --- SECURITY ---
def get_bot_token():
    if bot_instance:
        cog = bot_instance.get_cog('MusicBot')
        if cog: return cog.web_auth_token
    return None

@app.before_request
def check_auth():
    if request.path.startswith('/auth') or request.path.startswith('/cache'): return
    user_token = request.cookies.get('pi_music_auth')
    server_token = get_bot_token()
    if not server_token or user_token != server_token:
        return render_template_string("""
            <body style="background:#0f0f0f; color:#eee; font-family:sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; margin:0;">
                <h1 style="color:#ff4444; font-size:4rem; margin:0;">⛔</h1>
                <h2 style="margin-top:10px;">Access Denied</h2>
                <p style="color:#888;">Use <code>/link</code> in Discord to generate a secure key.</p>
            </body>
        """), 403

@app.route('/auth')
async def auth_route():
    token_from_url = request.args.get('token')
    server_token = get_bot_token()
    if token_from_url == server_token:
        resp = await make_response(redirect('/'))
        resp.set_cookie('pi_music_auth', token_from_url, max_age=86400)
        return resp
    return "❌ Invalid Token.", 403

@app.route('/cache/thumb/<path:filename>')
async def serve_thumbnail(filename):
    return await send_from_directory(CACHE_DIR, filename)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>PiMusic</title>
    <style>
        :root { --accent: #FFD700; --bg: #0f0f0f; --card: #1c1c1c; --text: #ffffff; --sec: #888; --danger: #ff4d4d; }
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 0; padding-bottom: 20px; }
        .header { padding: 20px; display: flex; align-items: center; justify-content: space-between; background: linear-gradient(180deg, rgba(0,0,0,0.4) 0%, transparent 100%); }
        .logo { font-size: 1.5rem; font-weight: 800; color: var(--accent); letter-spacing: -0.5px; }
        .status-badge { font-size: 0.75rem; padding: 4px 10px; border-radius: 20px; background: #333; color: #aaa; display:flex; align-items:center; gap:5px;}
        .status-badge.online { background: rgba(46, 204, 113, 0.2); color: #2ecc71; border: 1px solid rgba(46, 204, 113, 0.3); }
        .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
        .container { max-width: 500px; margin: 0 auto; padding: 0 15px; }
        .player-card { background: linear-gradient(145deg, #252525, #1a1a1a); padding: 25px; border-radius: 24px; text-align: center; box-shadow: 0 10px 30px rgba(0,0,0,0.3); border: 1px solid #333; margin-bottom: 25px; }
        .album-art { width: 150px; height: 150px; border-radius: 16px; margin: 0 auto 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.5); object-fit: cover; background: #222; }
        .track-title { font-size: 1.2rem; font-weight: 700; margin-bottom: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .track-meta { color: var(--sec); font-size: 0.9rem; font-weight: 500; }
        .controls { display: flex; justify-content: center; align-items: center; gap: 20px; margin-top: 25px; }
        .btn-ctrl { background: transparent; border: none; color: #eee; font-size: 1.5rem; cursor: pointer; padding: 10px; border-radius: 50%; transition: 0.2s; display: flex; align-items: center; justify-content: center; }
        .btn-play { background: var(--accent); color: #000; width: 60px; height: 60px; font-size: 1.8rem; box-shadow: 0 0 15px rgba(255, 215, 0, 0.3); }
        .btn-play:active { transform: scale(0.95); }
        .btn-sec:active { background: rgba(255,255,255,0.1); transform: scale(0.9); }
        .btn-auto.active { color: var(--accent); text-shadow: 0 0 10px var(--accent); }
        .search-wrap { position: relative; margin-bottom: 20px; }
        .search-input { width: 100%; padding: 16px 20px; padding-right: 50px; border-radius: 16px; border: none; background: #222; color: white; font-size: 1rem; box-shadow: inset 0 2px 5px rgba(0,0,0,0.2); transition: 0.2s; }
        .search-input:focus { outline: none; background: #2a2a2a; box-shadow: 0 0 0 2px var(--accent); }
        .search-btn { position: absolute; right: 8px; top: 8px; background: var(--accent); border: none; width: 36px; height: 36px; border-radius: 12px; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 1.1rem; }
        .tabs { display: flex; background: #1a1a1a; padding: 5px; border-radius: 14px; margin-bottom: 20px; }
        .tab-btn { flex: 1; border: none; background: transparent; color: #888; padding: 10px; font-weight: 600; border-radius: 10px; cursor: pointer; transition: 0.3s; }
        .tab-btn.active { background: #333; color: white; box-shadow: 0 2px 5px rgba(0,0,0,0.2); }
        .section { display: none; animation: fadeIn 0.3s; }
        .section.active { display: block; }
        .list-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 15px; background: #1c1c1c; border-radius: 12px; margin-bottom: 8px; border: 1px solid #2a2a2a; }
        .list-thumb { width: 40px; height: 40px; border-radius: 6px; object-fit: cover; margin-right: 12px; background: #000; flex-shrink: 0; }
        .item-info { flex: 1; overflow: hidden; margin-right: 10px; display:flex; align-items:center; }
        .item-text { overflow: hidden; }
        .item-title { font-weight: 600; font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .item-sub { font-size: 0.75rem; color: #666; margin-top: 2px; }
        .btn-del { border: none; background: rgba(255, 77, 77, 0.1); color: var(--danger); width: 30px; height: 30px; border-radius: 8px; cursor: pointer; font-size: 0.9rem; }
        .pl-tools { background: #222; padding: 15px; border-radius: 16px; margin-bottom: 15px; }
        .pl-inputs { display: flex; gap: 8px; margin-bottom: 10px; }
        .pl-input { flex: 1; background: #111; border: 1px solid #333; color: white; padding: 10px; border-radius: 8px; font-size: 0.9rem; }
        .btn-save { width: 100%; background: #333; color: white; border: none; padding: 10px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: 0.2s; }
        .btn-save:hover { background: var(--accent); color: black; }
        .res-item { display: flex; align-items: center; padding: 10px; background: #1a1a1a; margin-bottom: 10px; border-radius: 12px; cursor: pointer; border: 1px solid transparent; transition:0.2s; }
        .res-item:hover { border-color: var(--accent); background: #222; }
        .res-img { width: 50px; height: 50px; border-radius: 8px; object-fit: cover; margin-right: 12px; background: #000; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
        .spinner { width: 20px; height: 20px; border: 3px solid rgba(255,255,255,0.1); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin: 20px auto; display: none; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">🎵 PiMusic</div>
        <div id="status-badge" class="status-badge"><div class="dot"></div> <span id="status-text">Connecting...</span></div>
    </div>
    <div class="container">
        <div class="player-card">
            <img src="https://via.placeholder.com/150" id="np-img" class="album-art" onerror="this.src='https://via.placeholder.com/150?text=Music'">
            <div class="track-title" id="np-title">Nothing Playing</div>
            <div class="track-meta" id="np-meta">--:--</div>
            <div class="controls">
                <button class="btn-ctrl btn-sec btn-auto" id="btn-auto" onclick="control('autoplay')">♾️</button>
                <button class="btn-ctrl btn-sec" onclick="control('shuffle')">🔀</button>
                <button class="btn-ctrl btn-play" onclick="control('pause')">⏯</button>
                <button class="btn-ctrl btn-sec" onclick="control('skip')">⏭</button>
            </div>
        </div>
        <div class="search-wrap">
            <input type="text" class="search-input" id="urlInput" placeholder="Search..." onkeypress="handleEnter(event)">
            <button class="search-btn" onclick="searchSong()">🔍</button>
        </div>
        <div id="loading" class="spinner"></div>
        <div id="search-results"></div>
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('queue')">Queue <span id="q-count">(0)</span></button>
            <button class="tab-btn" onclick="switchTab('playlists')">Playlists</button>
        </div>
        <div id="tab-queue" class="section active"><div id="queue-list"></div></div>
        <div id="tab-playlists" class="section">
            <div class="pl-tools">
                <div class="pl-inputs">
                    <input type="text" class="pl-input" id="plName" placeholder="Name">
                    <input type="text" class="pl-input" id="plUrl" placeholder="Link (Opt)">
                </div>
                <button class="btn-save" onclick="savePlaylist()">Save</button>
            </div>
            <div id="playlist-list"></div>
        </div>
    </div>
    <script>
        function switchTab(tab) {
            document.querySelectorAll('.section').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active');
            event.target.classList.add('active');
        }
        function handleEnter(e) { if(e.key === 'Enter') searchSong(); }
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (res.status === 403) { window.location.reload(); return; }
                const data = await res.json();
                const badge = document.getElementById('status-badge');
                if (data.guild) {
                    badge.classList.add('online');
                    document.getElementById('status-text').innerText = data.guild;
                } else {
                    badge.classList.remove('online');
                    document.getElementById('status-text').innerText = "Offline";
                }
                document.getElementById('np-title').innerText = data.current ? data.current.title : "Nothing Playing";
                document.getElementById('np-meta').innerText = data.current ? `${data.current.author} • ${data.current.duration}` : "";
                const img = document.getElementById('np-img');
                const newSrc = data.current && data.current.thumbnail ? data.current.thumbnail : 'https://via.placeholder.com/150?text=Music';
                if(img.src !== newSrc && newSrc) img.src = newSrc;
                const autoBtn = document.getElementById('btn-auto');
                if (data.autoplay) autoBtn.classList.add('active'); else autoBtn.classList.remove('active');
                document.getElementById('q-count').innerText = `(${data.queue.length})`;
                const qList = document.getElementById('queue-list');
                qList.innerHTML = '';
                if(data.queue.length === 0) qList.innerHTML = '<div style="text-align:center; color:#555; padding:20px;">Queue is empty</div>';
                data.queue.forEach((track, index) => {
                    const div = document.createElement('div');
                    div.className = 'list-item';
                    const thumb = track.thumbnail ? track.thumbnail : 'https://via.placeholder.com/40';
                    div.innerHTML = `<div class="item-info"><img src="${thumb}" class="list-thumb"><div class="item-text"><div class="item-title">${index + 1}. ${track.title}</div></div></div><button class="btn-del" onclick="removeTrack(${index})">✕</button>`;
                    qList.appendChild(div);
                });
            } catch (e) { document.getElementById('status-badge').classList.remove('online'); }
        }
        async function fetchPlaylists() {
            try {
                const res = await fetch('/api/playlists');
                const data = await res.json();
                const list = document.getElementById('playlist-list');
                list.innerHTML = '';
                if(data.length === 0) { list.innerHTML = '<div style="text-align:center; color:#555;">No saved playlists</div>'; return; }
                data.forEach(pl => {
                    const icon = pl.type === 'live' ? '🔗' : '💾';
                    const sub = pl.type === 'live' ? 'Live Playlist' : `${pl.count} songs`;
                    const div = document.createElement('div');
                    div.className = 'list-item';
                    div.innerHTML = `<div class="item-info"><div class="item-text"><div class="item-title">${icon} ${pl.name}</div><div class="item-sub">${sub}</div></div></div><div style="display:flex; gap:10px;"><button class="btn-del" style="background:transparent; color:var(--accent); font-size:1.2rem;" onclick="loadPlaylist(this, '${pl.name}')">▶</button><button class="btn-del" onclick="deletePlaylist('${pl.name}')">🗑</button></div>`;
                    list.appendChild(div);
                });
            } catch (e) {}
        }
        async function searchSong() {
            const input = document.getElementById('urlInput');
            const resDiv = document.getElementById('search-results');
            const loader = document.getElementById('loading');
            if (!input.value.trim()) return;
            if (input.value.includes('http')) { addDirect(input.value); return; }
            resDiv.style.display = 'none'; resDiv.innerHTML = ''; loader.style.display = 'block';
            try {
                const res = await fetch('/api/search', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query: input.value}) });
                const results = await res.json();
                loader.style.display = 'none';
                if (results.error) { alert(results.error); return; }
                results.forEach(item => {
                    const div = document.createElement('div');
                    div.className = 'res-item';
                    div.onclick = () => { addDirect(item.url); resDiv.style.display='none'; document.getElementById('urlInput').value = ''; };
                    div.innerHTML = `<img src="${item.thumbnail}" class="res-img" onerror="this.src='https://via.placeholder.com/50'"><div class="item-info"><div class="item-text"><div class="item-title">${item.title}</div><div class="item-sub">${item.author} • ${item.duration}</div></div></div><div style="color:var(--accent); font-weight:bold;">+</div>`;
                    resDiv.appendChild(div);
                });
                resDiv.style.display = 'block';
            } catch (e) { loader.style.display = 'none'; alert("Search failed"); }
        }
        async function addDirect(url) {
            document.getElementById('urlInput').value = '';
            await fetch('/api/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query: url}) });
            fetchStatus();
        }
        async function control(action) { await fetch(`/api/control/${action}`, { method: 'POST' }); fetchStatus(); }
        async function removeTrack(index) { await fetch(`/api/remove/${index}`, { method: 'POST' }); fetchStatus(); }
        async function savePlaylist() {
            const name = document.getElementById('plName').value;
            const url = document.getElementById('plUrl').value;
            if(!name) return alert("Name required");
            const body = {name: name}; if(url) body.url = url;
            await fetch('/api/playlists/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
            document.getElementById('plName').value = ""; document.getElementById('plUrl').value = "";
            fetchPlaylists();
        }
        async function loadPlaylist(btn, name) {
            if(!confirm(`Load "${name}"?`)) return;
            const originalHTML = btn.innerHTML;
            btn.innerHTML = '⏳';
            await fetch('/api/playlists/load', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: name}) });
            fetchStatus();
            btn.innerHTML = originalHTML;
        }
        async function deletePlaylist(name) {
            if(!confirm("Delete?")) return;
            await fetch('/api/playlists/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: name}) });
            fetchPlaylists();
        }
        setInterval(fetchStatus, 2000);
        fetchStatus();
        fetchPlaylists();
    </script>
</body>
</html>
"""

def get_first_available_guild():
    if not bot_instance: return None
    if bot_instance.voice_clients: return bot_instance.voice_clients[0].guild
    if bot_instance.guilds: return bot_instance.guilds[0]
    return None

@app.route('/')
async def home(): return await render_template_string(DASHBOARD_HTML)

@app.route('/api/status')
async def api_status():
    guild = get_first_available_guild()
    if not guild: return jsonify({'current': None, 'queue': [], 'guild': None, 'autoplay': False})
    
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    
    current = None
    if state.current_track:
        current = {
            'title': state.current_track['title'],
            'author': state.current_track['author'],
            'duration': state.current_track['duration'],
            'thumbnail': get_thumbnail_url(state.current_track['id'])
        }
    
    queue_data = []
    for t in state.queue:
        queue_data.append({
            'title': t['title'],
            'id': t['id'],
            'thumbnail': get_thumbnail_url(t['id'])
        })
        
    return jsonify({'current': current, 'queue': queue_data, 'guild': guild.name, 'autoplay': state.autoplay})

@app.route('/api/playlists', methods=['GET'])
async def api_get_playlists():
    data = []
    for name, content in saved_playlists.items():
        if isinstance(content, list): data.append({'name': name, 'count': len(content), 'type': 'static'})
        elif isinstance(content, dict): data.append({'name': name, 'count': 0, 'type': 'live'})
    return jsonify(data)

@app.route('/api/playlists/save', methods=['POST'])
async def api_save_playlist():
    data = await request.get_json()
    name = data.get('name', '').lower()
    url = data.get('url', '')
    if not name: return jsonify({'error': 'No name'}), 400
    if url:
        saved_playlists[name] = {'type': 'live', 'url': url}
        save_json(PLAYLIST_FILE, saved_playlists)
        return jsonify({'status': 'ok'})
    # Static save logic
    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'No guild'}), 400
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    tracks = []
    if state.current_track: tracks.append(state.current_track)
    tracks.extend(state.queue)
    if not tracks: return jsonify({'error': 'Empty'}), 400
    clean = [{'id':t['id'], 'title':t['title'], 'author':t['author'], 'duration':t['duration'], 'duration_seconds':t['duration_seconds'], 'webpage':t['webpage']} for t in tracks]
    saved_playlists[name] = clean
    save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

@app.route('/api/playlists/load', methods=['POST'])
async def api_load_playlist():
    data = await request.get_json()
    name = data.get('name', '').lower()
    if name not in saved_playlists: return jsonify({'error': 'Not found'}), 404
    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'No guild'}), 400
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    content = saved_playlists[name]
    new_tracks = []
    
    if isinstance(content, list): new_tracks = content
    elif isinstance(content, dict):
        try:
            info = await bot_instance.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAYLIST_LOAD_OPTS).extract_info(content['url'], download=False))
            if 'entries' in info:
                for e in info['entries']:
                    if e: new_tracks.append({'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':f"https://www.youtube.com/watch?v={e['id']}"})
            asyncio.create_task(cog.load_rest_of_playlist(content['url'], guild.id))
        except: return jsonify({'error': 'Fetch fail'}), 500
        
    if new_tracks:
        state.queue.extend(new_tracks)
        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v): self.guild, self.voice_client, self.author = g, v, "WebUser"
                 async def send(self, *args, **kwargs): pass 
             await cog.play_next(DummyCtx(guild, guild.voice_client))
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Empty'}), 400

@app.route('/api/playlists/delete', methods=['POST'])
async def api_del_playlist():
    data = await request.get_json()
    if data['name'] in saved_playlists: del saved_playlists[data['name']]; save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

@app.route('/api/search', methods=['POST'])
async def api_search():
    data = await request.get_json()
    try:
        info = await bot_instance.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{data['query']}", download=False))
        res = []
        if 'entries' in info:
            for e in info['entries']:
                if e:
                    thumb = e.get('thumbnail')
                    if not thumb or not thumb.startswith('http'): thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
                    res.append({'title': e['title'], 'author': e['uploader'], 'duration': format_time(e['duration']), 'url': f"https://www.youtube.com/watch?v={e['id']}", 'thumbnail': thumb})
        return jsonify(res)
    except: return jsonify([]), 500

@app.route('/api/control/<action>', methods=['POST'])
async def api_control(action):
    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'No guild'}), 400
    vc = guild.voice_client
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    if action=='pause': 
        if vc.is_playing(): vc.pause() 
        elif vc.is_paused(): vc.resume()
    elif action=='skip': vc.stop()
    elif action=='shuffle': random.shuffle(state.queue)
    elif action=='autoplay': state.autoplay = not state.autoplay
    return jsonify({'status':'ok'})

@app.route('/api/remove/<int:index>', methods=['POST'])
async def api_remove(index):
    guild = get_first_available_guild()
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    if 0 <= index < len(state.queue): del state.queue[index]
    return jsonify({'status': 'ok'})

@app.route('/api/add', methods=['POST'])
async def api_add():
    data = await request.get_json()
    guild = get_first_available_guild()
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    query = data['query']
    if not re.match(r'^https?://', query): query = f"ytsearch1:{query}"
    
    if not state.last_text_channel: state.last_text_channel = guild.text_channels[0]

    try:
        info = await bot_instance.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
        def process(e): return {'id': e['id'], 'title': e['title'], 'author': e['uploader'], 'duration': format_time(e['duration']), 'duration_seconds': e['duration'], 'webpage': e['url']}
        
        if 'entries' in info: state.queue.append(process(info['entries'][0]))
        else: state.queue.append(process(info))
        
        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v): self.guild, self.voice_client, self.author = g, v, "WebUser"
                 async def send(self, *args, **kwargs): pass 
             await cog.play_next(DummyCtx(guild, guild.voice_client))
        return jsonify({'status':'ok'})
    except: return jsonify({'error':'fail'}), 500

# ==========================================
# 6. MAIN MUSIC BOT CLASS
# ==========================================

class MusicBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.states = {} 
        self.cleanup_loop.start()
        self.public_url = None
        self.web_auth_token = str(uuid4())
        
        global bot_instance
        bot_instance = bot 
        self.web_task = self.bot.loop.create_task(app.run_task(host='0.0.0.0', port=5000))

    async def cog_unload(self):
        self.cleanup_loop.stop()
        ngrok.kill()
        if self.web_task: self.web_task.cancel()

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if hasattr(ctx.command, 'on_error'): return
        if isinstance(error, commands.CommandNotFound): return
        if isinstance(error, commands.MissingPermissions): await ctx.send("❌ Permission denied")
        else:
            try: await ctx.send(f"❌ Error: {str(error)[:100]}")
            except: pass

    def get_state(self, guild_id):
        if guild_id not in self.states: self.states[guild_id] = ServerState()
        return self.states[guild_id]

    async def download_session_songs(self, tracks):
        if not tracks: return
        to_download = [t for t in tracks if not os.path.exists(f"{CACHE_DIR}/{t['id']}.webm")]
        if not to_download: return
        
        enforce_cache_limit()
        for track in to_download:
            enforce_cache_limit()
            try:
                await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_DOWNLOAD_OPTS).download([f'https://www.youtube.com/watch?v={track["id"]}']))
                cache_map[track['id']] = track['title']
                save_json(CACHE_MAP_FILE, cache_map)
            except Exception as e: log_error(f"DL Fail: {e}")
            await asyncio.sleep(0.5)

    async def stop_logic(self, guild_id):
        if status_led: status_led.off() 
        self.public_url = None
        if guild_id not in self.states: return
        guild = self.bot.get_guild(guild_id)
        state = self.states[guild_id]
        state.stopping = True
        if guild and guild.voice_client: await guild.voice_client.disconnect()
        all_tracks = state.session_new_tracks.copy()
        for t in state.queue: all_tracks[t['id']] = t
        if all_tracks: self.bot.loop.create_task(self.download_session_songs(list(all_tracks.values())))
        del self.states[guild_id]

    @tasks.loop(minutes=2)
    async def cleanup_loop(self):
        now = datetime.datetime.now()
        for gid in list(self.states.keys()):
            guild = self.bot.get_guild(gid)
            if not guild: del self.states[gid]; continue
            state = self.states[gid]
            if guild.voice_client:
                if len(guild.voice_client.channel.members) == 1 or (not guild.voice_client.is_playing() and (now - state.last_interaction).total_seconds() > 300):
                    await self.stop_logic(gid)

    def get_notification_channel(self, guild):
        if str(guild.id) in server_settings:
            ch_id = server_settings[str(guild.id)]
            ch = guild.get_channel(ch_id)
            if ch and ch.permissions_for(guild.me).send_messages: return ch
        state = self.get_state(guild.id)
        if state.last_text_channel: return state.last_text_channel
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                if any(x in ch.name.lower() for x in ['music', 'muzica', 'bot', 'general']): return ch
        return guild.text_channels[0] if guild.text_channels else None

    async def load_rest_of_playlist(self, url, guild_id):
        REST_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '51-', **COMMON_YDL_ARGS, 'noplaylist': False}
        try:
            info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(REST_OPTS).extract_info(url, download=False))
            if 'entries' in info:
                state = self.get_state(guild_id)
                count = 0
                for e in info['entries']:
                    if e: 
                        state.queue.append({'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':f"https://www.youtube.com/watch?v={e['id']}"})
                        count += 1
                guild = self.bot.get_guild(guild_id)
                ch = self.get_notification_channel(guild)
                if ch: await ch.send(f"✅ Loaded {count} more tracks in background.", silent=True)
        except: pass

    async def prepare_song(self, ctx, query):
        state = self.get_state(ctx.guild.id)
        state.last_interaction = datetime.datetime.now()
        state.stopping = False
        if hasattr(ctx, 'channel'): state.last_text_channel = ctx.channel
        
        if not ctx.voice_client:
            if ctx.author.voice: await ctx.author.voice.channel.connect()
            else: return await ctx.send("❌ Join VC")

        if ctx.interaction: await ctx.interaction.response.defer()
        
        info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
        def proc(e): return {'id': e['id'], 'title': e['title'], 'author': e['uploader'], 'duration': format_time(e['duration']), 'duration_seconds': e['duration'], 'webpage': e['url']}
        
        async def send_res(msg):
            if ctx.interaction: await ctx.interaction.followup.send(embed=discord.Embed(description=msg, color=COLOR_MAIN))
            else: await ctx.send(embed=discord.Embed(description=msg, color=COLOR_MAIN))

        if 'entries' in info: 
            state.queue.extend([proc(e) for e in info['entries'] if e])
            await send_res(f"✅ Added **{len(info['entries'])}** tracks.")
        else: 
            state.queue.append(proc(info))
            if ctx.voice_client.is_playing(): await send_res(f"✅ Queued: **{info['title']}**")
            
        if not ctx.voice_client.is_playing(): await self.play_next(ctx)

    async def play_next(self, ctx):
        state = self.get_state(ctx.guild.id)
        if state.stopping or not ctx.guild.voice_client: return
        if state.processing_next: return
        
        if not state.queue and state.autoplay and state.history:
            last = state.history[-1]
            try:
                info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_MIX_OPTS).extract_info(f"https://www.youtube.com/watch?v={last['id']}&list=RD{last['id']}", download=False))
                if 'entries' in info:
                    for e in info['entries']:
                        if e['id'] != last['id']: 
                            state.queue.append({'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':e['url']})
                            break
            except: pass

        if state.queue:
            state.processing_next = True 
            next_song = state.queue.pop(0)
            state.current_track = next_song
            state.history.append(next_song)
            if len(state.history) > 20: state.history.pop(0)

            try:
                local = os.path.abspath(f"{CACHE_DIR}/{next_song['id']}.webm")
                play_local = os.path.exists(local) and os.path.getsize(local) > 1024
                
                thumb_local = f"{CACHE_DIR}/{next_song['id']}.jpg"
                if play_local and not os.path.exists(thumb_local):
                    try: await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL({'writethumbnail':True, 'skip_download':True, 'outtmpl': f'{CACHE_DIR}/%(id)s.%(ext)s', 'quiet':True}).download([f"https://www.youtube.com/watch?v={next_song['id']}"]))
                    except: pass

                if play_local:
                    os.utime(local, None)
                    source = await discord.FFmpegOpusAudio.from_probe(local, **FFMPEG_LOCAL_OPTS)
                else:
                    state.session_new_tracks[next_song['id']] = next_song
                    info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAY_OPTS).extract_info(next_song['id'], download=False))
                    opts = FFMPEG_STREAM_OPTS.copy()
                    if 'http_headers' in info:
                        opts['before_options'] = f'-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -headers "{ "".join([f"{k}: {v}\r\n" for k,v in info["http_headers"].items()]) }" -nostdin'
                    source = await discord.FFmpegOpusAudio.from_probe(info['url'], **opts)

                ctx.voice_client.play(source, after=lambda e: self.bot.loop.create_task(self.play_next(ctx)))
                if status_led: status_led.on()
                state.processing_next = False 
                
                embed = discord.Embed(title="🎶 Now Playing", description=f"**[{next_song['title']}]({next_song['webpage']})**", color=COLOR_MAIN)
                embed.set_thumbnail(url=f"https://i.ytimg.com/vi/{next_song['id']}/mqdefault.jpg")
                embed.add_field(name="Author", value=next_song['author'])
                embed.add_field(name="Duration", value=next_song['duration'])
                
                ch = self.get_notification_channel(ctx.guild)
                if ch: await ch.send(embed=embed, view=MusicControlView(self, ctx.guild.id))
            
            except Exception as e: 
                log_error(f"Playback error: {e}")
                state.processing_next = False
                await asyncio.sleep(2) 
                self.bot.loop.create_task(self.play_next(ctx))
        else:
            state.current_track = None
            state.processing_next = False
            if status_led: status_led.off()

    # --- COMMANDS ---
    @commands.hybrid_command(name="help", description="Show all commands")
    async def help(self, ctx):
        embed = discord.Embed(title="🎵 PiMusic Bot Commands", description="Control your music with these commands:", color=COLOR_MAIN)
        embed.add_field(name="🎵 Music", value="`/play [song/url]` - Play music\n`/pause` / `/resume`\n`/skip`\n`/stop`\n`/autoplay`", inline=False)
        embed.add_field(name="🎛️ Dashboard", value="`/link` - Get Web Panel\n`/setchannel` - Set output channel", inline=False)
        embed.add_field(name="📂 Playlists", value="`/saveplaylist`\n`/loadplaylist`\n`/listplaylists`\n`/delplaylist`", inline=False)
        embed.add_field(name="📜 Queue", value="`/queue`\n`/history`\n`/shuffle`", inline=False)
        embed.add_field(name="⚙️ Utils", value="`/search`\n`/cache`\n`/dash`", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def sync(self, ctx):
        await ctx.bot.tree.sync()
        await ctx.send("✅ Synced! Commands will appear shortly.")

    @commands.hybrid_command(name="setchannel")
    async def set_channel(self, ctx):
        server_settings[str(ctx.guild.id)] = ctx.channel.id
        save_json(SETTINGS_FILE, server_settings)
        embed = discord.Embed(description=f"✅ Bound to {ctx.channel.mention}", color=COLOR_MAIN)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="link")
    async def link(self, ctx):
        if not self.public_url:
             http_tunnel = await self.bot.loop.run_in_executor(None, lambda: ngrok.connect("127.0.0.1:5000", bind_tls=True))
             self.public_url = http_tunnel.public_url
        
        secure_link = f"{self.public_url}/auth?token={self.web_auth_token}"
        embed = discord.Embed(title="🎛️ Web Dashboard", description="Click below to open the control panel.", color=COLOR_MAIN)
        view = ui.View()
        view.add_item(ui.Button(label="Open Dashboard", url=secure_link))
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="play")
    async def play(self, ctx, search: str):
        await self.prepare_song(ctx, search if 'http' in search else f"ytsearch1:{search}")

    @commands.hybrid_command(name="stop")
    async def stop(self, ctx): 
        await self.stop_logic(ctx.guild.id)
        embed = discord.Embed(description="👋 Stopped.", color=COLOR_MAIN)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="skip")
    async def skip(self, ctx): 
        ctx.voice_client.stop()
        embed = discord.Embed(description="⏭️ Skipped.", color=COLOR_MAIN)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="queue")
    async def queue(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.current_track and not state.queue:
            return await ctx.send(embed=discord.Embed(description="Queue empty.", color=COLOR_MAIN))
        view = ListPaginator(state.queue, title="Server Queue", is_queue=True, current=state.current_track)
        await ctx.send(embed=view.get_embed(), view=view)

    @commands.hybrid_command(name="pause")
    async def pause(self, ctx): 
        if ctx.voice_client.is_playing(): 
            ctx.voice_client.pause()
            await ctx.send(embed=discord.Embed(description="⏸️ Paused.", color=COLOR_MAIN))

    @commands.hybrid_command(name="resume")
    async def resume(self, ctx): 
        if ctx.voice_client.is_paused(): 
            ctx.voice_client.resume()
            await ctx.send(embed=discord.Embed(description="▶️ Resumed.", color=COLOR_MAIN))

    @commands.hybrid_command(name="shuffle")
    async def shuffle(self, ctx):
        random.shuffle(self.get_state(ctx.guild.id).queue)
        await ctx.send(embed=discord.Embed(description="🔀 Shuffled.", color=COLOR_MAIN))

    @commands.hybrid_command(name="saveplaylist")
    async def saveplaylist(self, ctx, name: str, url: str = None):
        if url: saved_playlists[name] = {'type': 'live', 'url': url}
        else:
            state = self.get_state(ctx.guild.id)
            tracks = []
            if state.current_track: tracks.append(state.current_track)
            tracks.extend(state.queue)
            if not tracks: return await ctx.send(embed=discord.Embed(description="Queue empty.", color=discord.Color.red()))
            clean = [{'id':t['id'], 'title':t['title'], 'author':t['author'], 'duration':t['duration'], 'duration_seconds':t['duration_seconds'], 'webpage':t['webpage']} for t in tracks]
            saved_playlists[name] = clean
        save_json(PLAYLIST_FILE, saved_playlists)
        await ctx.send(embed=discord.Embed(description=f"💾 Saved **{name}**.", color=COLOR_MAIN))

    @commands.hybrid_command(name="loadplaylist")
    async def loadplaylist(self, ctx, name: str):
        if name not in saved_playlists: return await ctx.send(embed=discord.Embed(description="❌ Not found.", color=discord.Color.red()))
        content = saved_playlists[name]
        state = self.get_state(ctx.guild.id)
        
        if isinstance(content, list):
            state.queue.extend(content)
            await ctx.send(embed=discord.Embed(description=f"📂 Loaded **{len(content)}** songs.", color=COLOR_MAIN))
        elif isinstance(content, dict):
            await ctx.send(embed=discord.Embed(description="🔄 Loading live playlist (First 50)...", color=COLOR_MAIN))
            try:
                info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAYLIST_LOAD_OPTS).extract_info(content['url'], download=False))
                tracks = [{'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':e['webpage']} for e in info['entries'] if e]
                state.queue.extend(tracks)
                await ctx.send(embed=discord.Embed(description=f"✅ Loaded **{len(tracks)}**. Rest loading in BG...", color=COLOR_MAIN))
                asyncio.create_task(self.load_rest_of_playlist(content['url'], ctx.guild.id))
            except: await ctx.send(embed=discord.Embed(description="❌ Error loading.", color=discord.Color.red()))

        if not ctx.voice_client:
            if ctx.author.voice: await ctx.author.voice.channel.connect()
        if not ctx.voice_client.is_playing(): await self.play_next(ctx)

    @commands.hybrid_command(name="listplaylists")
    async def listplaylists(self, ctx):
        msg = "\n".join([f"{k}" for k in saved_playlists.keys()])
        await ctx.send(embed=discord.Embed(title="📂 Saved Playlists", description=msg if msg else "None", color=COLOR_MAIN))

    @commands.hybrid_command(name="delplaylist")
    async def delplaylist(self, ctx, name: str):
        if name in saved_playlists: 
            del saved_playlists[name]
            save_json(PLAYLIST_FILE, saved_playlists)
            await ctx.send(embed=discord.Embed(description=f"🗑️ Deleted **{name}**.", color=COLOR_MAIN))
        else: await ctx.send(embed=discord.Embed(description="❌ Not found.", color=discord.Color.red()))

    @commands.hybrid_command(name="cache")
    async def cache_list(self, ctx):
        valid = [f for f in os.listdir(CACHE_DIR) if f.endswith('.webm')]
        data = [{'title': cache_map.get(f.replace('.webm',''), f), 'duration': 'Cached'} for f in valid]
        if not data: return await ctx.send(embed=discord.Embed(description="Cache empty.", color=COLOR_MAIN))
        data.sort(key=lambda x: x['title'])
        view = ListPaginator(data, title="Local Cache", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view)

    @commands.hybrid_command(name="dash")
    async def dash(self, ctx):
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        try: temp = os.popen("vcgencmd measure_temp").readline().replace("temp=","").strip()
        except: temp = "N/A"
        count = len([n for n in os.listdir(CACHE_DIR) if n.endswith('.webm')])
        size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in os.listdir(CACHE_DIR) if f.endswith('.webm')) / (1024**3)
        embed = discord.Embed(title="🚀 Pi Stats", color=COLOR_MAIN)
        embed.add_field(name="System", value=f"CPU: `{cpu}%` | RAM: `{ram}%` | `{temp}`")
        embed.add_field(name="Storage", value=f"`{count}` songs | `{size:.2f} GB` / {MAX_CACHE_SIZE_GB} GB")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="search")
    async def search(self, ctx, query: str):
        await ctx.interaction.response.defer()
        info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{query}", download=False))
        await ctx.interaction.followup.send("🔎 **Results:**", view=SelectionView(info['entries'], self, ctx))

    @commands.hybrid_command(name="history")
    async def history(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.history: return await ctx.send(embed=discord.Embed(description="History empty.", color=COLOR_MAIN))
        view = ListPaginator(list(reversed(state.history)), title="History", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view)

    @commands.hybrid_command(name="autoplay")
    async def autoplay(self, ctx):
        state = self.get_state(ctx.guild.id)
        state.autoplay = not state.autoplay
        await ctx.send(embed=discord.Embed(description=f"📻 Auto-Play: **{'ON' if state.autoplay else 'OFF'}**", color=COLOR_MAIN))

# ==========================================
# 7. STARTUP
# ==========================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='$', intents=intents)
bot.remove_command('help') 

@bot.event
async def on_ready():
    log_info(f'Logged in as {bot.user}')
    global bot_instance
    bot_instance = bot 
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="$help"))

async def main():
    try:
        async with bot:
            await bot.add_cog(MusicBot(bot))
            await bot.start(TOKEN)
    except KeyboardInterrupt: pass
    finally:
        if not bot.is_closed(): await bot.close()
        ngrok.kill()

if __name__ == "__main__":
    asyncio.run(main())
