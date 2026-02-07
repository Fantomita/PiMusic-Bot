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
from quart import Quart, render_template_string, request, jsonify, make_response, redirect
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
YDL_DOWNLOAD_OPTS = {'format': 'bestaudio[ext=webm]/bestaudio/best', 'outtmpl': f'{CACHE_DIR}/%(id)s.%(ext)s', **COMMON_YDL_ARGS}

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
            if entry.is_file() and entry.name.endswith('.webm'):
                total_size += entry.stat().st_size
                files.append(entry)
    if total_size > max_bytes:
        files.sort(key=lambda x: x.stat().st_mtime)
        for entry in files:
            try:
                size = entry.stat().st_size
                os.remove(entry.path)
                total_size -= size
                vid_id = entry.name.replace('.webm', '')
                if vid_id in cache_map: del cache_map[vid_id]
                if total_size <= (max_bytes - 100 * 1024 * 1024): break
            except: pass
        save_json(CACHE_MAP_FILE, cache_map)

# ==========================================
# 4. WEB DASHBOARD (QUART & SECURITY)
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
    if request.path == '/auth': return
    user_token = request.cookies.get('pi_music_auth')
    server_token = get_bot_token()
    if not server_token or user_token != server_token:
        return render_template_string("""
            <body style="background:#121212; color:white; font-family:sans-serif; text-align:center; padding-top:50px;">
                <h1>⛔ Access Denied</h1>
                <p>Use <code>$link</code> or <code>/link</code> in Discord to get access.</p>
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

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>PiMusic</title>
    <style>
        :root { --accent: #FFD700; --bg: #121212; --card: #1e1e1e; --text: #e0e0e0; --sec-text: #a0a0a0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 10px; display: flex; flex-direction: column; align-items: center; }
        .header { display: flex; align-items: center; gap: 10px; margin-bottom: 15px; }
        .status-dot { height: 8px; width: 8px; background-color: #555; border-radius: 50%; box-shadow: 0 0 5px #555; transition: 0.3s; }
        .online { background-color: #00ff88; box-shadow: 0 0 8px #00ff88; }
        h1 { margin: 0; font-size: 1.2rem; color: var(--accent); letter-spacing: 1px; }
        
        .container { width: 100%; max-width: 480px; }
        .card { background-color: var(--card); padding: 15px; border-radius: 16px; margin-bottom: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.3); border: 1px solid #333; }
        
        .now-playing { text-align: center; }
        .track-title { font-size: 1.1rem; font-weight: 700; color: #fff; margin: 5px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .track-meta { color: var(--sec-text); font-size: 0.85rem; }
        
        .controls { display: flex; justify-content: center; gap: 15px; margin-top: 15px; }
        .btn-ctrl { background: #333; border: none; color: #fff; width: 50px; height: 50px; border-radius: 50%; font-size: 1.2rem; cursor: pointer; transition: 0.2s; display: flex; align-items: center; justify-content: center; }
        .btn-ctrl:active { transform: scale(0.9); }
        .btn-active { background-color: var(--accent) !important; color: #000 !important; box-shadow: 0 0 10px var(--accent); }
        
        .search-box { position: relative; display: flex; gap: 8px; }
        input { width: 100%; padding: 12px 15px; border-radius: 25px; border: 1px solid #444; background: #2a2a2a; color: white; font-size: 1rem; outline: none; }
        input:focus { border-color: var(--accent); }
        .btn-search { background: var(--accent); border: none; border-radius: 50%; width: 42px; height: 42px; font-size: 1.2rem; cursor: pointer; color: #000; position: absolute; right: 2px; top: 2px; display: flex; align-items: center; justify-content: center; }
        
        .list-container { list-style: none; padding: 0; margin: 0; max-height: 250px; overflow-y: auto; }
        .list-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #333; font-size: 0.9rem; }
        .list-item:last-child { border-bottom: none; }
        
        .action-btn { background: none; border: none; font-size: 1.1rem; cursor: pointer; padding: 5px; margin-left: 5px; }
        .btn-load { color: var(--accent); }
        .btn-del { color: #ff4444; }

        #search-results { display: none; margin-top: 10px; animation: fadeIn 0.3s; }
        .result-item { display: flex; align-items: center; padding: 10px; background: #252525; margin-bottom: 8px; border-radius: 10px; cursor: pointer; transition: 0.2s; border: 1px solid transparent; }
        .result-item:hover { background: #333; border-color: var(--accent); }
        .thumb { width: 60px; height: 45px; border-radius: 6px; object-fit: cover; margin-right: 12px; background: #000; flex-shrink: 0; }
        .res-info { flex: 1; overflow: hidden; }
        .res-title { font-size: 0.95rem; font-weight: bold; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #fff; }
        .res-artist { font-size: 0.8rem; color: #bbb; }
        
        .spinner { border: 3px solid rgba(255,255,255,0.1); border-top: 3px solid var(--accent); border-radius: 50%; width: 20px; height: 20px; animation: spin 0.8s linear infinite; margin: 10px auto; display: none; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-5px); } to { opacity: 1; transform: translateY(0); } }
        
        .playlist-input-group { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap;}
        .btn-save-pl { background-color: #333; color: white; border: 1px solid #444; border-radius: 8px; padding: 8px 15px; cursor: pointer; flex:1; }
        .btn-save-pl:hover { background-color: var(--accent); color: black; }
        .pl-type-icon { margin-right:5px; font-size:1.1em; }
    </style>
</head>
<body>
    <div class="header">
        <span id="status-indicator" class="status-dot"></span>
        <h1>PiMusic</h1>
    </div>

    <div class="container">
        <div class="card now-playing">
            <div id="status-text" style="font-size:0.75rem; color:#666; margin-bottom:5px;">Connecting...</div>
            <div class="track-title" id="np-title">Nothing Playing</div>
            <div class="track-meta" id="np-meta">--:--</div>
            <div class="controls">
                <button class="btn-ctrl" onclick="control('shuffle')" title="Shuffle">🔀</button>
                <button class="btn-ctrl" onclick="control('pause')" title="Play/Pause">⏯</button>
                <button class="btn-ctrl" onclick="control('skip')" title="Skip">⏭</button>
                <button id="btn-autoplay" class="btn-ctrl" onclick="control('autoplay')" title="Toggle Autoplay">♾️</button>
            </div>
        </div>

        <div class="card">
            <div class="search-box">
                <input type="text" id="urlInput" placeholder="Search song..." onkeypress="handleEnter(event)">
                <button class="btn-search" onclick="searchSong()">🔍</button>
            </div>
            <div id="loading" class="spinner"></div>
            <div id="search-results"></div>
        </div>

        <div class="card">
            <h3 style="margin:0 0 10px 0; font-size:1rem; color:var(--sec-text);">Saved Playlists</h3>
            <div class="playlist-input-group">
                <input type="text" id="plNameInput" placeholder="Name..." style="flex:1;">
                <input type="text" id="plLinkInput" placeholder="Optional URL (Live)" style="flex:2;">
            </div>
            <div style="display:flex; gap:8px; margin-bottom:10px;">
                <button class="btn-save-pl" onclick="savePlaylist()">Save</button>
            </div>
            <ul class="list-container" id="playlist-list"></ul>
        </div>

        <div class="card">
            <h3 style="margin:0 0 10px 0; font-size:1rem; color:var(--sec-text);">Next Up (<span id="q-count">0</span>)</h3>
            <ul class="list-container" id="queue-list"></ul>
        </div>
    </div>

    <script>
        function handleEnter(e) { if(e.key === 'Enter') searchSong(); }

        // --- CORE ---
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (res.status === 403) { window.location.reload(); return; }
                const data = await res.json();
                
                const dot = document.getElementById('status-indicator');
                if (data.guild) {
                    dot.classList.add('online');
                    document.getElementById('status-text').innerText = data.guild;
                } else {
                    dot.classList.remove('online');
                    document.getElementById('status-text').innerText = "Offline / No VC";
                }

                document.getElementById('np-title').innerText = data.current ? data.current.title : "Nothing Playing";
                document.getElementById('np-meta').innerText = data.current ? `${data.current.author} • ${data.current.duration}` : "";
                
                const autoBtn = document.getElementById('btn-autoplay');
                if (data.autoplay) autoBtn.classList.add('btn-active');
                else autoBtn.classList.remove('btn-active');

                // Queue
                document.getElementById('q-count').innerText = data.queue.length;
                const list = document.getElementById('queue-list');
                list.innerHTML = '';
                data.queue.forEach((track, index) => {
                    const li = document.createElement('li');
                    li.className = 'list-item';
                    li.innerHTML = `<span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:85%;"><b>${index+1}.</b> ${track.title}</span> <button class="action-btn btn-del" onclick="removeTrack(${index})">✕</button>`;
                    list.appendChild(li);
                });
            } catch (e) { document.getElementById('status-indicator').classList.remove('online'); }
        }

        // --- PLAYLISTS ---
        async function fetchPlaylists() {
            try {
                const res = await fetch('/api/playlists');
                const data = await res.json();
                const list = document.getElementById('playlist-list');
                list.innerHTML = '';
                
                if (data.length === 0) {
                    list.innerHTML = '<li class="list-item" style="color:#666; justify-content:center;">No playlists saved</li>';
                    return;
                }

                data.forEach(pl => {
                    const icon = pl.type === 'live' ? '🔗' : '💾';
                    const count = pl.type === 'live' ? 'Live' : `(${pl.count} songs)`;
                    const li = document.createElement('li');
                    li.className = 'list-item';
                    li.innerHTML = `
                        <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:70%;"><span class="pl-type-icon">${icon}</span>${pl.name} <small style="color:#888">${count}</small></span> 
                        <div>
                            <button class="action-btn btn-load" title="Load" onclick="loadPlaylist(this, '${pl.name}')">📂</button>
                            <button class="action-btn btn-del" title="Delete" onclick="deletePlaylist('${pl.name}')">🗑️</button>
                        </div>`;
                    list.appendChild(li);
                });
            } catch (e) {}
        }

        async function savePlaylist() {
            const name = document.getElementById('plNameInput').value;
            const url = document.getElementById('plLinkInput').value;
            
            if (!name) return alert("Enter a name first!");
            
            const body = {name: name};
            if(url) body.url = url; 

            const res = await fetch('/api/playlists/save', { 
                method: 'POST', 
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            const data = await res.json();
            if(data.error) alert(data.error);
            else {
                document.getElementById('plNameInput').value = "";
                document.getElementById('plLinkInput').value = "";
                fetchPlaylists();
            }
        }

        async function loadPlaylist(btn, name) {
            if(!confirm(`Load "${name}"? This will add to the current queue.`)) return;
            const originalText = btn.innerText;
            btn.innerText = "⏳";
            
            try {
                const res = await fetch('/api/playlists/load', { 
                    method: 'POST', 
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                });
                const data = await res.json();
                if(data.error) alert(data.error);
                else fetchStatus();
            } catch(e) {
                alert("Timeout or Error loading playlist");
            }
            btn.innerText = originalText;
        }

        async function deletePlaylist(name) {
            if(!confirm(`Delete playlist "${name}"?`)) return;
            await fetch('/api/playlists/delete', { 
                method: 'POST', 
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name})
            });
            fetchPlaylists();
        }

        // --- SEARCH ---
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
                    div.className = 'result-item';
                    div.onclick = () => addDirect(item.url);
                    div.innerHTML = `<img src="${item.thumbnail}" class="thumb"><div class="res-info"><div class="res-title">${item.title}</div><div class="res-artist">${item.author} • ${item.duration}</div></div><div class="res-add">+</div>`;
                    resDiv.appendChild(div);
                });
                resDiv.style.display = 'block';
            } catch (e) { loader.style.display = 'none'; alert("Search failed."); }
        }

        async function addDirect(url) {
            const input = document.getElementById('urlInput');
            const resDiv = document.getElementById('search-results');
            resDiv.style.display = 'none';
            input.value = ""; input.placeholder = "Adding...";
            await fetch('/api/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query: url}) });
            input.placeholder = "Search song..."; fetchStatus();
        }

        async function control(action) { await fetch(`/api/control/${action}`, { method: 'POST' }); fetchStatus(); }
        async function removeTrack(index) { await fetch(`/api/remove/${index}`, { method: 'POST' }); fetchStatus(); }

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
    if not cog: return jsonify({'current': None, 'queue': [], 'guild': "Bot Loading...", 'autoplay': False})
    state = cog.get_state(guild.id)
    current = None
    if state.current_track: current = {'title': state.current_track['title'], 'author': state.current_track['author'], 'duration': state.current_track['duration']}
    queue_data = [{'title': t['title'], 'id': t['id']} for t in state.queue]
    return jsonify({'current': current, 'queue': queue_data, 'guild': guild.name, 'autoplay': state.autoplay})

# --- PLAYLIST API ---
@app.route('/api/playlists', methods=['GET'])
async def api_get_playlists():
    data = []
    for name, content in saved_playlists.items():
        if isinstance(content, list):
            data.append({'name': name, 'count': len(content), 'type': 'static'})
        elif isinstance(content, dict):
            data.append({'name': name, 'count': 0, 'type': 'live'})
    return jsonify(data)

@app.route('/api/playlists/save', methods=['POST'])
async def api_save_playlist():
    data = await request.get_json()
    name = data.get('name', '').lower()
    url = data.get('url', '')
    
    if not name: return jsonify({'error': 'No name'}), 400
    
    if url:
        if 'youtube.com' not in url and 'youtu.be' not in url:
             return jsonify({'error': 'Invalid YouTube URL'}), 400
        saved_playlists[name] = {'type': 'live', 'url': url}
        save_json(PLAYLIST_FILE, saved_playlists)
        return jsonify({'status': 'ok'})

    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'No active guild'}), 400
    
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    
    tracks_to_save = []
    if state.current_track:
        tracks_to_save.append({
            'id': state.current_track['id'],
            'title': state.current_track['title'],
            'author': state.current_track['author'],
            'duration': state.current_track['duration'],
            'duration_seconds': state.current_track.get('duration_seconds', 0),
            'webpage': state.current_track.get('webpage'),
        })
    for t in state.queue:
        tracks_to_save.append({
            'id': t['id'],
            'title': t['title'],
            'author': t['author'],
            'duration': t['duration'],
            'duration_seconds': t.get('duration_seconds', 0),
            'webpage': t.get('webpage'),
        })
    
    if not tracks_to_save: return jsonify({'error': 'Queue is empty'}), 400
    
    saved_playlists[name] = tracks_to_save
    save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

@app.route('/api/playlists/load', methods=['POST'])
async def api_load_playlist():
    data = await request.get_json()
    name = data.get('name', '').lower()
    if name not in saved_playlists: return jsonify({'error': 'Playlist not found'}), 404
    
    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'No active guild'}), 400
    
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    content = saved_playlists[name]
    
    # 1. LOAD FIRST 50 (SYNC)
    first_50_tracks = []
    rest_url = None
    
    if isinstance(content, list):
        first_50_tracks = content
    elif isinstance(content, dict) and content.get('type') == 'live':
        rest_url = content['url']
        try:
            # Load 1-50
            FAST_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '1-50', **COMMON_YDL_ARGS, 'noplaylist': False}
            info = await bot_instance.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(FAST_OPTS).extract_info(content['url'], download=False))
            if 'entries' in info:
                for entry in info['entries']:
                    if not entry: continue
                    first_50_tracks.append({
                        'id': entry.get('id'),
                        'title': entry.get('title', 'Unknown'),
                        'author': entry.get('uploader', 'Artist'),
                        'duration': format_time(entry.get('duration', 0)),
                        'duration_seconds': entry.get('duration', 0),
                        'webpage': f"https://www.youtube.com/watch?v={entry.get('id')}"
                    })
        except Exception as e:
            return jsonify({'error': f"Fetch failed: {str(e)[:50]}"}), 500

    # Add first 50 to queue
    if first_50_tracks:
        state.queue.extend(first_50_tracks)
        
        # Start play if idle
        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v): self.guild, self.voice_client, self.author = g, v, "WebUser"
                 async def send(self, *args, **kwargs): pass 
             await cog.play_next(DummyCtx(guild, guild.voice_client))
        
        # 2. TRIGGER BACKGROUND LOADING (For Live Playlists)
        if rest_url:
            asyncio.create_task(cog.load_rest_of_playlist(rest_url, guild.id))

        return jsonify({'status': 'ok'})
    else:
        return jsonify({'error': 'Playlist empty or fetch failed'}), 400

@app.route('/api/playlists/delete', methods=['POST'])
async def api_del_playlist():
    data = await request.get_json()
    name = data.get('name', '').lower()
    if name in saved_playlists:
        del saved_playlists[name]
        save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

# --- OTHER API ---
@app.route('/api/search', methods=['POST'])
async def api_search():
    data = await request.get_json()
    query = data.get('query')
    if not query: return jsonify({'error': 'Empty query'}), 400
    try:
        info = await bot_instance.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{query}", download=False))
        results = []
        if 'entries' in info:
            for entry in info['entries']:
                if not entry: continue
                vid_id = entry.get('id')
                thumb = entry.get('thumbnail')
                if not thumb or not thumb.startswith('http'): thumb = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
                results.append({'title': entry.get('title', 'Unknown'), 'author': entry.get('uploader', 'Unknown'), 'duration': format_time(entry.get('duration', 0)), 'url': f"https://www.youtube.com/watch?v={vid_id}", 'thumbnail': thumb})
        return jsonify(results)
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/control/<action>', methods=['POST'])
async def api_control(action):
    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'Bot not in VC'}), 400
    vc = guild.voice_client
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    if action == 'pause':
        if vc and vc.is_playing(): vc.pause()
        elif vc and vc.is_paused(): vc.resume()
    elif action == 'skip':
        if vc: vc.stop()
    elif action == 'shuffle': random.shuffle(state.queue)
    elif action == 'autoplay': state.autoplay = not state.autoplay
    return jsonify({'status': 'ok'})

@app.route('/api/remove/<int:index>', methods=['POST'])
async def api_remove(index):
    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'No guild'}), 400
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    if 0 <= index < len(state.queue): del state.queue[index]
    return jsonify({'status': 'ok'})

@app.route('/api/add', methods=['POST'])
async def api_add():
    data = await request.get_json()
    query = data.get('query')
    if not query: return jsonify({'error': 'No query'}), 400
    guild = get_first_available_guild()
    if not guild: return jsonify({'error': 'No guild found'}), 400
    cog = bot_instance.get_cog('MusicBot')
    state = cog.get_state(guild.id)
    
    if not re.match(r'^https?://', query): query = f"ytsearch1:{query}"
    
    # Canal
    if not state.last_text_channel:
        if str(guild.id) in server_settings: state.last_text_channel = guild.get_channel(server_settings[str(guild.id)])
        if not state.last_text_channel:
            for ch in guild.text_channels:
                if any(x in ch.name.lower() for x in ['music', 'muzica', 'bot', 'general']): state.last_text_channel = ch; break
        if not state.last_text_channel: state.last_text_channel = guild.text_channels[0]

    try:
        info = await bot_instance.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
        def process(entry):
            return {'id': entry.get('id'), 'title': entry.get('title', 'Unknown'), 'author': entry.get('uploader', 'Artist'), 'duration': format_time(entry.get('duration')), 'duration_seconds': entry.get('duration', 0), 'webpage': entry.get('url') if 'http' in entry.get('url', '') else f"https://www.youtube.com/watch?v={entry.get('id')}"}
        if 'entries' in info:
            first_entry = info['entries'][0]
            if first_entry: state.queue.append(process(first_entry))
        else: state.queue.append(process(info))
        
        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v): self.guild, self.voice_client, self.author = g, v, "WebUser"
                 async def send(self, *args, **kwargs): pass 
             await cog.play_next(DummyCtx(guild, guild.voice_client))
        return jsonify({'status': 'ok'})
    except Exception as e: return jsonify({'error': str(e)}), 500

# ==========================================
# 5. DISCORD UI CLASSES
# ==========================================

class ServerState:
    def __init__(self):
        self.queue = []
        self.current_track = None
        self.last_interaction = datetime.datetime.now()
        self.session_new_tracks = {}
        self.processing_next = False 
        self.history = []
        self.autoplay = False
        self.stopping = False
        self.last_text_channel = None 

class SelectionMenu(ui.Select):
    def __init__(self, entries, cog, ctx):
        options = []
        for entry in entries[:10]:
            title = (entry.get('title', 'Unknown')[:90] + '..') if len(entry.get('title', '')) > 90 else entry.get('title', 'Unknown')
            val = entry.get('id') or entry.get('url')
            if val: options.append(discord.SelectOption(label=title, value=val))
        super().__init__(placeholder="Select a song...", options=options)
        self.cog, self.ctx = cog, ctx
    async def callback(self, interaction):
        if interaction.user != self.ctx.author: return
        await interaction.response.edit_message(content="✅ **Confirmed.**", view=None)
        await self.cog.prepare_song(self.ctx, self.values[0])

class SelectionView(ui.View):
    def __init__(self, entries, cog, ctx):
        super().__init__(timeout=30)
        self.add_item(SelectionMenu(entries, cog, ctx))
        self.message = None

class MusicControlView(ui.View):
    def __init__(self, cog, guild_id):
        super().__init__(timeout=None)
        self.cog, self.guild_id = cog, guild_id
    @ui.button(emoji="⏯️", style=discord.ButtonStyle.blurple)
    async def play_pause(self, interaction, button):
        vc = interaction.guild.voice_client
        if vc: 
            if vc.is_paused(): vc.resume()
            elif vc.is_playing(): vc.pause()
        await interaction.response.defer()
    @ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, button):
        if interaction.guild.voice_client: interaction.guild.voice_client.stop()
        await interaction.response.defer()
    @ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction, button):
        state = self.cog.get_state(self.guild_id)
        random.shuffle(state.queue)
        await interaction.response.send_message("🔀 Shuffled queue!", ephemeral=True)
    @ui.button(emoji="📋", style=discord.ButtonStyle.gray)
    async def q_btn(self, interaction, button):
        state = self.cog.get_state(self.guild_id)
        if not state.current_track and not state.queue:
            return await interaction.response.send_message("Queue empty!", ephemeral=True)
        view = ListPaginator(state.queue, title="Server Queue", is_queue=True, current=state.current_track)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)
    @ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction, button):
        await self.cog.stop_logic(self.guild_id)
        await interaction.response.send_message("👋 Stopping & Saving...", ephemeral=True)

class ListPaginator(ui.View):
    def __init__(self, data_list, title="List", is_queue=True, current=None):
        super().__init__(timeout=60)
        self.data_list = data_list
        self.title = title
        self.is_queue = is_queue
        self.current = current
        self.page = 0
        self.items_per_page = 10
        self.max_pages = max(0, (len(data_list) - 1) // self.items_per_page)

    def get_embed(self):
        embed = discord.Embed(title=f"📜 {self.title}", color=COLOR_MAIN)
        if self.is_queue and self.current:
            source = "💾 Local" if os.path.exists(f"{CACHE_DIR}/{self.current['id']}.webm") else "☁️ Stream"
            embed.add_field(name=f"▶️ Now Playing ({source})", value=f"**{self.current['title']}**", inline=False)
        start = self.page * self.items_per_page
        end = start + self.items_per_page
        if not self.data_list: desc = "Empty."
        else:
            desc_lines = []
            for i, s in enumerate(self.data_list[start:end]):
                if isinstance(s, dict): line = f"`{start+i+1}.` **{s['title']}** ({s.get('duration', '?:??')})"
                else: line = f"`{start+i+1}.` {s}"
                desc_lines.append(line)
            desc = "\n".join(desc_lines)
        embed.description = desc
        embed.set_footer(text=f"Page {self.page+1}/{self.max_pages+1} • Total: {len(self.data_list)}")
        return embed

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.gray)
    async def prev(self, interaction, button):
        if self.page > 0: self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
    @ui.button(emoji="➡️", style=discord.ButtonStyle.gray)
    async def next(self, interaction, button):
        if self.page < self.max_pages: self.page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

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
        log_info("🛑 Shutting down...")
        self.cleanup_loop.stop()
        if status_led: status_led.off()
        ngrok.kill()
        if self.web_task:
            self.web_task.cancel()
            try: await self.web_task
            except asyncio.CancelledError: pass

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if hasattr(ctx.command, 'on_error'): return
        if isinstance(error, commands.CommandNotFound): return
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Missing permissions.")
        else:
            err_msg = str(error)
            if hasattr(error, 'original'): err_msg = str(error.original)
            log_error(f"Error: {err_msg}")
            try: await ctx.send(f"❌ **Error:** `{err_msg[:200]}`")
            except: pass

    def get_state(self, guild_id):
        if guild_id not in self.states: self.states[guild_id] = ServerState()
        return self.states[guild_id]

    async def download_session_songs(self, tracks):
        if not tracks: return
        to_download = [t for t in tracks if not os.path.exists(f"{CACHE_DIR}/{t['id']}.webm")]
        if not to_download: return
        enforce_cache_limit()
        for i, track in enumerate(to_download):
            enforce_cache_limit()
            try:
                await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_DOWNLOAD_OPTS).download([f'https://www.youtube.com/watch?v={track["id"]}']))
                cache_map[track['id']] = track['title']
                save_json(CACHE_MAP_FILE, cache_map)
            except Exception as e: log_error(f"Download failed for {track.get('title')}: {e}")
            await asyncio.sleep(0.5)

    async def stop_logic(self, guild_id):
        if status_led: status_led.off() 
        self.public_url = None
        if guild_id not in self.states: return
        guild = self.bot.get_guild(guild_id)
        state = self.states[guild_id]
        state.stopping = True
        if guild and guild.voice_client: await guild.voice_client.disconnect()
        all_tracks_to_save = state.session_new_tracks.copy()
        for track in state.queue: all_tracks_to_save[track['id']] = track
        tracks_to_save = list(all_tracks_to_save.values())
        del self.states[guild_id]
        if tracks_to_save: self.bot.loop.create_task(self.download_session_songs(tracks_to_save))

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

    # --- SMART LOAD: Rest of Playlist ---
    async def load_rest_of_playlist(self, url, guild_id):
        # Starts from index 51 to end
        REST_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '51-', **COMMON_YDL_ARGS, 'noplaylist': False}
        log_info(f"BG Load started for {url}")
        
        try:
            info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(REST_OPTS).extract_info(url, download=False))
            added_count = 0
            if 'entries' in info:
                state = self.get_state(guild_id)
                for entry in info['entries']:
                    if not entry: continue
                    state.queue.append({
                        'id': entry.get('id'),
                        'title': entry.get('title', 'Unknown'),
                        'author': entry.get('uploader', 'Artist'),
                        'duration': format_time(entry.get('duration', 0)),
                        'duration_seconds': entry.get('duration', 0),
                        'webpage': f"https://www.youtube.com/watch?v={entry.get('id')}"
                    })
                    added_count += 1
            
            # Notify on success
            guild = self.bot.get_guild(guild_id)
            if guild:
                ch = self.get_notification_channel(guild)
                if ch: await ch.send(f"✅ **Background Load Complete:** Added {added_count} more tracks from playlist.", silent=True)
                
        except Exception as e:
            log_error(f"BG Load Failed: {e}")

    async def prepare_song(self, ctx, query):
        state = self.get_state(ctx.guild.id)
        state.last_interaction = datetime.datetime.now()
        state.stopping = False
        
        if hasattr(ctx, 'channel'): state.last_text_channel = ctx.channel
        
        async def send_msg(content, silent=True):
            if ctx.interaction and not ctx.interaction.response.is_done(): await ctx.send(content, ephemeral=silent)
            else: await ctx.send(content, silent=silent)

        if not ctx.voice_client:
            if ctx.author.voice:
                try: await ctx.author.voice.channel.connect()
                except Exception as e: return await send_msg(f"❌ Error joining VC: `{str(e)}`", silent=False)
            else: return await send_msg("❌ You must be in a VC!", silent=False)

        if ctx.interaction and not ctx.interaction.response.is_done(): await ctx.interaction.response.defer()
        elif hasattr(ctx, 'typing'): await ctx.typing()

        try:
            info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
            def process(entry):
                return {'id': entry.get('id'), 'title': entry.get('title', 'Unknown'), 'author': entry.get('uploader', 'Artist'), 'duration': format_time(entry.get('duration')), 'duration_seconds': entry.get('duration', 0), 'webpage': entry.get('url') if 'http' in entry.get('url', '') else f"https://www.youtube.com/watch?v={entry.get('id')}"}
            
            if 'entries' in info:
                new_tracks = [process(e) for e in info['entries'] if e]
                state.queue.extend(new_tracks)
                await send_msg(f"✅ Added **{len(new_tracks)}** tracks.")
            else:
                song = process(info)
                state.queue.append(song)
                is_busy = ctx.voice_client and (ctx.voice_client.is_playing() or state.processing_next)
                if is_busy: await send_msg(f"✅ Queued: **{song['title']}**")
            
            if not ctx.voice_client.is_playing() and not state.processing_next: 
                await self.play_next(ctx)
        
        except Exception as e:
            log_error(f"Prepare error: {e}")
            await send_msg(f"❌ Error loading: `{str(e)[:100]}`", silent=False)

    async def play_next(self, ctx):
        state = self.get_state(ctx.guild.id)
        state.last_interaction = datetime.datetime.now()
        if state.stopping or not ctx.guild.voice_client or not ctx.guild.voice_client.is_connected():
            if status_led: status_led.off()
            return
        if state.processing_next: return
        
        notify_channel = self.get_notification_channel(ctx.guild)

        if not state.queue and state.autoplay and state.history:
            try:
                last = state.history[-1]
                info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_MIX_OPTS).extract_info(f"https://www.youtube.com/watch?v={last['id']}&list=RD{last['id']}", download=False))
                if 'entries' in info:
                    for e in info['entries']:
                        if not any(h['id'] == e['id'] for h in state.history) and e['id'] != last['id'] and not is_too_similar(e['title'], last['title']):
                            state.queue.append({'id': e['id'], 'title': e['title'], 'author': e['uploader'], 'duration': format_time(e['duration']), 'duration_seconds': e['duration'], 'webpage': e['url']})
                            if notify_channel: await notify_channel.send(f"📻 **Auto-Play:** Added **{e['title']}**", silent=True)
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
                embed.add_field(name="👤 Author", value=next_song['author'], inline=True)
                embed.add_field(name="⏳ Duration", value=f"`{next_song['duration']}`", inline=True)
                embed.set_footer(text=f"Source: {'💾 Local' if play_local else '☁️ Stream'}")
                if notify_channel: 
                    try: await notify_channel.send(embed=embed, view=MusicControlView(self, ctx.guild.id), silent=True)
                    except: pass
            
            except Exception as e: 
                log_error(f"Playback error: {e}")
                state.processing_next = False
                if notify_channel: await notify_channel.send(f"❌ Failed to play **{next_song['title']}**: `{str(e)[:100]}`. Skipping...", silent=False)
                await asyncio.sleep(2) 
                self.bot.loop.create_task(self.play_next(ctx))
        else:
            state.current_track = None
            state.processing_next = False
            if status_led: status_led.off()

    # --- HYBRID COMMANDS ---
    
    @commands.hybrid_command(name="help", description="Show all commands")
    async def help(self, ctx):
        embed = discord.Embed(title="🎵 PiMusic Bot Commands", description="Control your music with these commands:", color=COLOR_MAIN)
        embed.add_field(name="🎵 Music", value="`/play [song/url]` - Play music\n`/pause` / `/resume` - Pause/Resume\n`/skip` - Skip song\n`/stop` - Disconnect & Clear\n`/autoplay` - Toggle Radio Mode", inline=False)
        embed.add_field(name="🎛️ Dashboard", value="`/link` - Get Web Control Panel\n`/setchannel` - Bind bot to text channel", inline=False)
        embed.add_field(name="📂 Playlists", value="`/saveplaylist [name] [url]` - Save queue or link\n`/loadplaylist [name]` - Load saved playlist\n`/listplaylists` - View playlists\n`/delplaylist [name]` - Delete playlist", inline=False)
        embed.add_field(name="📜 Queue", value="`/queue` - Show queue\n`/history` - Show recently played\n`/shuffle` - Shuffle queue", inline=False)
        embed.add_field(name="⚙️ Utils", value="`/search [query]` - Search & Select\n`/cache` - View downloaded songs\n`/dash` - View Pi Stats", inline=False)
        embed.set_footer(text="Running on Raspberry Pi Zero 2W")
        await ctx.send(embed=embed)

    @commands.command()
    async def sync(self, ctx):
        await ctx.bot.tree.sync()
        await ctx.send("✅ Synced! Commands will appear shortly.")

    @commands.hybrid_command(name="setchannel", description="Set this channel for bot notifications")
    async def set_channel(self, ctx):
        server_settings[str(ctx.guild.id)] = ctx.channel.id
        save_json(SETTINGS_FILE, server_settings)
        await ctx.send(f"✅ **Bound to {ctx.channel.mention}**! I will send all music updates here.")

    @commands.hybrid_command(name="link", aliases=['dashboard', 'web', 'panel'], description="Get the link to the Web Control Panel")
    async def link(self, ctx):
        # Auto-Join
        if not ctx.voice_client:
            if ctx.author.voice:
                try: await ctx.author.voice.channel.connect()
                except Exception as e: return await ctx.send(f"❌ Could not join your VC: `{str(e)}`")
            else:
                await ctx.send("⚠️ Note: I'm not in a voice channel. Use `/play` or join one.")

        if NGROK_TOKEN:
            try: ngrok.set_auth_token(NGROK_TOKEN)
            except: pass
        await ctx.send("🔄 **Starting dashboard...**")
        try:
            if not self.public_url:
                http_tunnel = await self.bot.loop.run_in_executor(None, lambda: ngrok.connect("127.0.0.1:5000", bind_tls=True))
                self.public_url = http_tunnel.public_url
            
            secure_link = f"{self.public_url}/auth?token={self.web_auth_token}"
            await ctx.send(f"🎛️ **Dashboard Link:**\n[Click here to open Control Panel]({secure_link})\n*Link valid until bot restart.*")
        
        except Exception as e: await ctx.send(f"❌ Ngrok Error: {str(e)}")

    @commands.hybrid_command(name="play", aliases=['p'], description="Play a song from YouTube")
    @app_commands.describe(search="The song name or URL")
    async def play(self, ctx, *, search: str):
        q = search if re.match(r'^https?://', search) else f"ytsearch1:{search}"
        await self.prepare_song(ctx, q)

    @commands.hybrid_command(name="stop", description="Stop music and clear queue")
    async def stop(self, ctx):
        await self.stop_logic(ctx.guild.id)
        await ctx.send("👋 Stopped.")

    @commands.hybrid_command(name="pause", description="Pause playback")
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Paused.")

    @commands.hybrid_command(name="resume", description="Resume playback")
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Resumed.")

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.queue: return await ctx.send("Queue empty.")
        random.shuffle(state.queue)
        await ctx.send("🔀 Shuffled!")

    @commands.hybrid_command(name="queue", aliases=['q'], description="Show the music queue")
    async def queue(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.current_track and not state.queue: return await ctx.send("Queue empty.")
        view = ListPaginator(state.queue, title="Server Queue", is_queue=True, current=state.current_track)
        await ctx.send(embed=view.get_embed(), view=view)

    @commands.hybrid_command(name="cache", description="List cached songs")
    async def cache_list(self, ctx):
        valid = [f for f in os.listdir(CACHE_DIR) if f.endswith('.webm')]
        data = [{'title': cache_map.get(f.replace('.webm',''), f), 'duration': 'Cached'} for f in valid]
        if not data: return await ctx.send("Cache empty.")
        data.sort(key=lambda x: x['title'])
        view = ListPaginator(data, title="Local Cache", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view)

    @commands.hybrid_command(name="dash", description="Show Pi Hardware Stats")
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

    @commands.hybrid_command(name="skip", aliases=['s'], description="Skip current song")
    async def skip(self, ctx):
        if ctx.voice_client: ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")

    @commands.hybrid_command(name="search", description="Search YouTube and select result")
    @app_commands.describe(query="Song to search for")
    async def search(self, ctx, *, query: str):
        if not ctx.voice_client:
            if ctx.author.voice:
                try: await ctx.author.voice.channel.connect()
                except Exception as e: return await ctx.send(f"❌ Error joining VC: `{str(e)}`")
            else: return await ctx.send("❌ You must be in a VC.")

        if ctx.interaction: await ctx.interaction.response.defer()
        
        info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{query}", download=False))
        if not info.get('entries'): return await ctx.send("❌ No results.")
        
        if ctx.interaction:
            await ctx.interaction.followup.send("🔎 **Results:**", view=SelectionView(info['entries'], self, ctx))
        else:
            await ctx.send("🔎 **Results:**", view=SelectionView(info['entries'], self, ctx))
    
    @commands.hybrid_command(name="history", description="Show played history")
    async def history(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.history: return await ctx.send("History empty.")
        view = ListPaginator(list(reversed(state.history)), title="History", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view)

    @commands.hybrid_command(name="autoplay", description="Toggle Auto-Play")
    async def autoplay(self, ctx):
        state = self.get_state(ctx.guild.id)
        state.autoplay = not state.autoplay
        await ctx.send(f"📻 Auto-Play: **{'ON' if state.autoplay else 'OFF'}**")

    # --- Playlist Commands (Discord) ---
    @commands.hybrid_command(name="saveplaylist", description="Save queue (or link) as playlist")
    @app_commands.describe(name="Name of the playlist", url="Optional YouTube Playlist URL")
    async def saveplaylist(self, ctx, name: str, url: str = None):
        name = name.lower()
        if url:
            saved_playlists[name] = {'type': 'live', 'url': url}
            save_json(PLAYLIST_FILE, saved_playlists)
            await ctx.send(f"🔗 Linked playlist **{name}** to URL.")
        else:
            state = self.get_state(ctx.guild.id)
            tracks = []
            if state.current_track: tracks.append(state.current_track)
            tracks.extend(state.queue)
            if not tracks: return await ctx.send("Queue empty. Provide a URL to save a live playlist.")
            
            clean_tracks = []
            for t in tracks:
                clean_tracks.append({
                    'id': t['id'], 'title': t['title'], 'author': t.get('author', 'Unknown'),
                    'duration': t.get('duration', '0:00'), 'duration_seconds': t.get('duration_seconds', 0),
                    'webpage': t.get('webpage', '')
                })
            saved_playlists[name] = clean_tracks
            save_json(PLAYLIST_FILE, saved_playlists)
            await ctx.send(f"💾 Saved queue as **{name}** ({len(tracks)} songs).")

    @commands.hybrid_command(name="loadplaylist", description="Load a saved playlist")
    @app_commands.describe(name="Name of the playlist")
    async def loadplaylist(self, ctx, name: str):
        name = name.lower()
        if name not in saved_playlists: return await ctx.send("❌ Playlist not found.")
        
        content = saved_playlists[name]
        
        if isinstance(content, dict) and content.get('type') == 'live':
            if ctx.interaction: await ctx.interaction.response.defer()
            else: await ctx.send(f"🔄 Fetching live playlist **{name}** (First 50)...")
            
            try:
                FAST_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '1-50', **COMMON_YDL_ARGS, 'noplaylist': False}
                info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(FAST_OPTS).extract_info(content['url'], download=False))
                tracks = []
                if 'entries' in info:
                    for entry in info['entries']:
                        if not entry: continue
                        tracks.append({
                            'id': entry.get('id'),
                            'title': entry.get('title', 'Unknown'),
                            'author': entry.get('uploader', 'Artist'),
                            'duration': format_time(entry.get('duration', 0)),
                            'duration_seconds': entry.get('duration', 0),
                            'webpage': f"https://www.youtube.com/watch?v={entry.get('id')}"
                        })
                
                state = self.get_state(ctx.guild.id)
                state.queue.extend(tracks)
                
                msg = f"📂 Loaded **{name}** (Started with {len(tracks)} songs). Loading rest in background..."
                if ctx.interaction: await ctx.interaction.followup.send(msg)
                else: await ctx.send(msg)

                # TRIGGER BG LOAD
                asyncio.create_task(self.load_rest_of_playlist(content['url'], ctx.guild.id))

            except Exception as e:
                msg = f"❌ Failed to fetch playlist: {str(e)[:100]}"
                if ctx.interaction: await ctx.interaction.followup.send(msg)
                else: await ctx.send(msg)
                return

        else:
            state = self.get_state(ctx.guild.id)
            state.queue.extend(content)
            await ctx.send(f"📂 Loaded **{name}** ({len(content)} songs).")

        state = self.get_state(ctx.guild.id)
        if not ctx.voice_client:
            if ctx.author.voice: await ctx.author.voice.channel.connect()
        if ctx.voice_client and not ctx.voice_client.is_playing() and not state.processing_next:
            await self.play_next(ctx)

    @commands.hybrid_command(name="listplaylists", description="List all saved playlists")
    async def listplaylists(self, ctx):
        if not saved_playlists: return await ctx.send("No playlists saved.")
        lines = []
        for k, v in saved_playlists.items():
            if isinstance(v, list): lines.append(f"💾 **{k}** ({len(v)} songs)")
            elif isinstance(v, dict): lines.append(f"🔗 **{k}** (Live Link)")
        await ctx.send(f"**📂 Saved Playlists:**\n" + "\n".join(lines))

    @commands.hybrid_command(name="delplaylist", description="Delete a saved playlist")
    @app_commands.describe(name="Name of the playlist")
    async def delplaylist(self, ctx, name: str):
        name = name.lower()
        if name in saved_playlists:
            del saved_playlists[name]
            save_json(PLAYLIST_FILE, saved_playlists)
            await ctx.send(f"🗑️ Deleted playlist **{name}**.")
        else:
            await ctx.send("❌ Playlist not found.")

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
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="$help or /help"))
    try:
        synced = await bot.tree.sync()
        log_info(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        log_error(f"Sync error: {e}")

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
