import asyncio
import datetime
import json
import logging
import os
import platform
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4

# --- Third Party Imports ---
import discord
import psutil
import requests
import yt_dlp
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv
from quart import Quart, jsonify, make_response, redirect, render_template_string, request, send_from_directory

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

# --- Logging Setup ---
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

# --- System Optimization ---
sys.dont_write_bytecode = True
try: os.nice(-15)  # Higher priority for audio process
except: pass

# --- Environment Variables ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    log_error("❌ ERROR: DISCORD_TOKEN missing.")
    sys.exit(1)

# --- File Paths ---
CACHE_DIR = './music_cache'
CACHE_MAP_FILE = 'cache_map.json'
PLAYLIST_FILE = 'playlists.json'
SETTINGS_FILE = 'server_settings.json'
MAX_CACHE_SIZE_GB = 16

if not os.path.exists(CACHE_DIR): os.makedirs(CACHE_DIR)

# ==========================================
# 2. AUDIO & DOWNLOADER SETTINGS
# ==========================================

COLOR_MAIN = 0xFFD700  # Gold

# --- FFmpeg Options ---
FFMPEG_STREAM_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin',
    'options': '-vn -threads 2 -bufsize 8192k'
}
FFMPEG_LOCAL_OPTS = {
    'options': '-vn -threads 2 -bufsize 8192k'
}

# --- yt-dlp Options ---
COMMON_YDL_ARGS = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'socket_timeout': 30
}

# 1. Play: Stream high quality audio
YDL_PLAY_OPTS = {
    'format': 'bestaudio[ext=webm]/bestaudio/best',
    **COMMON_YDL_ARGS
}

# 2. Flat: Fast metadata fetch (good for playlists/URLs)
YDL_FLAT_OPTS = {
    'extract_flat': 'in_playlist',
    **COMMON_YDL_ARGS
}

# 3. Search: Deep metadata fetch (good for keywords to get accurate video URL)
YDL_SEARCH_OPTS = {
    'extract_flat': False,
    **COMMON_YDL_ARGS
}

# 4. Mix: For Autoplay/Radio mode
YDL_MIX_OPTS = {
    'extract_flat': 'in_playlist',
    'playlist_items': '1-20',
    **COMMON_YDL_ARGS,
    'noplaylist': False
}

# 5. Download: Save to cache with thumbnail
YDL_DOWNLOAD_OPTS = {
    'format': 'bestaudio[ext=webm]/bestaudio/best',
    'outtmpl': f'{CACHE_DIR}/%(id)s.%(ext)s',
    'writethumbnail': True,
    'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
    **COMMON_YDL_ARGS
}

# 6. Playlist Load: Batch loading
YDL_PLAYLIST_LOAD_OPTS = {
    'extract_flat': 'in_playlist',
    'playlist_items': '1-50',
    **COMMON_YDL_ARGS,
    'noplaylist': False
}

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================

def load_json(filename):
    """Safely loads a JSON file."""
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def save_json(filename, data):
    """Safely saves data to a JSON file."""
    try:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=4)
    except OSError as e:
        log_error(f"Failed to save JSON to {filename}: {e}")

# Load Initial State
cache_map = load_json(CACHE_MAP_FILE)
saved_playlists = load_json(PLAYLIST_FILE)
server_settings = load_json(SETTINGS_FILE)

def format_time(seconds):
    """Formats seconds into MM:SS or HH:MM:SS."""
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"

def _enforce_cache_limit_sync():
    """Deletes old cached files if the directory exceeds the size limit (Synchronous)."""
    max_bytes = MAX_CACHE_SIZE_GB * 1024 * 1024 * 1024
    files = []
    total_size = 0
    
    # Scan directory for usage
    with os.scandir(CACHE_DIR) as it:
        for entry in it:
            if entry.is_file():
                total_size += entry.stat().st_size
                if entry.name.endswith('.webm'):
                    files.append(entry)
    
    if total_size > max_bytes:
        # Sort by modification time (oldest first)
        files.sort(key=lambda x: x.stat().st_mtime)
        
        for entry in files:
            try:
                os.remove(entry.path)
                
                # Try to remove associated thumbnail
                thumb_path = entry.path.replace('.webm', '.jpg')
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                
                total_size -= entry.stat().st_size
                vid_id = entry.name.replace('.webm', '')
                
                if vid_id in cache_map:
                    del cache_map[vid_id]
                
                # Stop if we are safely under the limit (buffer of 100MB)
                if total_size <= (max_bytes - 100 * 1024 * 1024):
                    break
            except OSError as e:
                log_error(f"Error cleaning cache file {entry.name}: {e}")
                
        save_json(CACHE_MAP_FILE, cache_map)

async def enforce_cache_limit(loop):
    """Async wrapper for cache limit enforcement."""
    await loop.run_in_executor(None, _enforce_cache_limit_sync)

def get_thumbnail_url(vid_id):
    """Returns local thumbnail path if cached, else remote URL."""
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

# --- Dashboard HTML Template ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>PiMusic Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #FFD700;
            --primary-dim: rgba(255, 215, 0, 0.2);
            --bg-dark: #0a0a0a;
            --bg-card: #141414;
            --bg-card-hover: #1f1f1f;
            --text-main: #ffffff;
            --text-muted: #888888;
            --border: #2a2a2a;
            --danger: #ff4d4d;
            --radius: 20px;
            --shadow: 0 10px 40px -10px rgba(0,0,0,0.5);
        }

        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        
        html, body {
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg-dark);
            color: var(--text-main);
            margin: 0;
            padding: 0;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            overflow-x: hidden; /* Fix horizontal scroll */
        }

        /* Ambient Background */
        body::before {
            content: '';
            position: fixed;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle at center, rgba(255, 215, 0, 0.03) 0%, transparent 60%);
            z-index: -1;
            pointer-events: none;
        }

        .header {
            padding: 20px 25px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            backdrop-filter: blur(10px);
            position: sticky;
            top: 0;
            z-index: 100;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }

        .logo {
            font-size: 1.5rem;
            font-weight: 800;
            background: linear-gradient(135deg, #fff 0%, var(--primary) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }

        .status-badge {
            font-size: 0.75rem;
            font-weight: 600;
            padding: 6px 12px;
            border-radius: 30px;
            background: rgba(255,255,255,0.05);
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 8px;
            border: 1px solid transparent;
            transition: 0.3s;
        }

        .status-badge.online {
            background: rgba(46, 204, 113, 0.1);
            color: #2ecc71;
            border-color: rgba(46, 204, 113, 0.2);
        }

        .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 10px currentColor; }

        .container {
            max-width: 1200px;
            width: 100%;
            margin: 0 auto;
            padding: 20px;
            flex: 1;
            display: grid;
            grid-template-columns: 400px 1fr;
            gap: 30px;
            align-items: start;
        }

        .left-col {
            position: sticky;
            top: 100px;
        }

        .right-col {
            min-width: 0; /* Fix grid overflow */
        }

        /* Player Card */
        .player-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 30px;
            text-align: center;
            box-shadow: var(--shadow);
            position: relative;
            overflow: hidden;
            margin-bottom: 25px;
        }

        .album-art-container {
            position: relative;
            width: 100%;
            padding-bottom: 100%; /* Square aspect ratio */
            margin: 0 auto 25px;
            border-radius: 20px;
            overflow: hidden;
        }

        .album-art {
            position: absolute;
            top: 0; left: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            transition: transform 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275);
        }
        
        .player-card:hover .album-art { transform: scale(1.05); }

        .track-info { margin-bottom: 30px; }
        .track-title { 
            font-size: 1.6rem; 
            font-weight: 700; 
            margin-bottom: 8px; 
            line-height: 1.2;
            white-space: nowrap; 
            overflow: hidden; 
            text-overflow: ellipsis; 
            letter-spacing: -0.02em;
        }
        .track-meta { 
            color: var(--text-muted); 
            font-size: 1rem; 
            font-weight: 500; 
            background: rgba(255,255,255,0.03);
            display: inline-block;
            padding: 6px 16px;
            border-radius: 12px;
        }

        /* Controls */
        .controls {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 20px;
        }

        .btn-ctrl {
            background: rgba(255,255,255,0.05);
            border: none;
            color: var(--text-main);
            width: 55px;
            height: 55px;
            border-radius: 50%;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.4rem;
        }

        .btn-ctrl:hover { background: rgba(255,255,255,0.1); transform: translateY(-2px); }
        .btn-ctrl:active { transform: scale(0.95); }

        .btn-play {
            background: var(--primary);
            color: #000;
            width: 75px;
            height: 75px;
            font-size: 2rem;
            box-shadow: 0 10px 30px rgba(255, 215, 0, 0.3);
        }
        .btn-play:hover { background: #ffe033; box-shadow: 0 15px 35px rgba(255, 215, 0, 0.4); }

        .btn-auto.active { 
            background: var(--primary-dim); 
            color: var(--primary); 
            border: 1px solid var(--primary);
        }

        /* Search */
        .search-wrap { 
            position: relative; 
            margin-bottom: 25px; 
            z-index: 10;
        }
        
        .search-input {
            width: 100%;
            padding: 18px 60px 18px 25px;
            border-radius: 18px;
            border: 1px solid var(--border);
            background: var(--bg-card);
            color: white;
            font-size: 1rem;
            font-family: inherit;
            transition: 0.3s;
            box-shadow: inset 0 2px 10px rgba(0,0,0,0.2);
        }

        .search-input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px var(--primary-dim);
            background: #1a1a1a;
        }

        .search-btn {
            position: absolute;
            right: 10px;
            top: 10px;
            bottom: 10px;
            width: 45px;
            background: var(--primary);
            border: none;
            border-radius: 12px;
            cursor: pointer;
            color: #000;
            font-size: 1.1rem;
            transition: 0.2s;
            display:flex; align-items:center; justify-content:center;
        }
        .search-btn:hover { transform: scale(1.05); }

        /* Tabs */
        .tabs {
            display: flex;
            background: rgba(255,255,255,0.03);
            padding: 5px;
            border-radius: 16px;
            margin-bottom: 20px;
            position: sticky;
            top: 90px;
            z-index: 90;
            backdrop-filter: blur(10px);
        }

        .tab-btn {
            flex: 1;
            border: none;
            background: transparent;
            color: var(--text-muted);
            padding: 14px;
            font-size: 1rem;
            font-weight: 600;
            border-radius: 12px;
            cursor: pointer;
            transition: 0.3s;
            font-family: inherit;
        }

        .tab-btn.active {
            background: var(--bg-card);
            color: var(--text-main);
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        }

        /* Lists */
        .section { display: none; animation: slideUp 0.4s ease; }
        .section.active { display: block; }

        @keyframes slideUp {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .list-item, .res-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            margin-bottom: 10px;
            transition: 0.2s;
        }
        
        .res-item { cursor: pointer; }

        .list-item:hover, .res-item:hover {
            transform: translateY(-2px);
            border-color: #444;
            background: var(--bg-card-hover);
        }

        .list-thumb, .res-img {
            width: 50px;
            height: 50px;
            border-radius: 12px;
            object-fit: cover;
            margin-right: 15px;
            background: #222;
            flex-shrink: 0;
        }

        .item-info { flex: 1; overflow: hidden; margin-right: 15px; display: flex; align-items: center; }
        .item-text { overflow: hidden; }
        .item-title { font-weight: 600; font-size: 1rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
        .item-sub { font-size: 0.85rem; color: var(--text-muted); }

        .tag-auto {
            font-size: 0.7rem;
            background: var(--primary);
            color: #000;
            padding: 2px 8px;
            border-radius: 6px;
            font-weight: 700;
            margin-left: 8px;
            text-transform: uppercase;
        }

        .btn-del {
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: rgba(255, 77, 77, 0.1);
            color: var(--danger);
            border: none;
            cursor: pointer;
            display: flex; align-items: center; justify-content: center;
            transition: 0.2s;
            font-size: 1.1rem;
        }
        .btn-del:hover { background: var(--danger); color: white; }

        .btn-action {
            background: rgba(255,255,255,0.05);
            color: var(--text-main);
            width: 40px; height: 40px; border-radius: 12px; border:none; cursor:pointer;
            display: flex; align-items: center; justify-content: center;
            margin-right: 8px; transition: 0.2s;
            font-size: 1.1rem;
        }
        .btn-action:hover { background: var(--primary); color:black; }

        /* Loader */
        .spinner {
            width: 30px; height: 30px;
            border: 3px solid rgba(255,255,255,0.1);
            border-top-color: var(--primary);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 30px auto;
            display: none;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* Playlist Tools */
        .pl-tools { background: var(--bg-card); padding: 20px; border-radius: 20px; margin-bottom: 20px; border: 1px solid var(--border); }
        .pl-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        .pl-input { background: #0a0a0a; border: 1px solid #333; color: white; padding: 12px; border-radius: 12px; font-family: inherit; width: 100%; box-sizing: border-box; }
        .btn-save { width: 100%; padding: 12px; background: #333; color: white; border: none; border-radius: 12px; font-weight: 600; cursor: pointer; transition:0.2s; }
        .btn-save:hover { background: var(--primary); color:black; }

        /* Mobile Optimization */
        @media (max-width: 900px) {
            .container { grid-template-columns: 1fr; padding: 15px; gap: 20px; }
            .left-col { position: static; }
            .player-card { padding: 25px; }
            .album-art-container { width: 250px; padding-bottom: 250px; }
            .track-title { font-size: 1.4rem; }
            .tabs { top: 85px; }
            .pl-inputs { grid-template-columns: 1fr; } /* Stack inputs on mobile */
        }

    </style>
</head>
<body>
    <div class="header">
        <div class="logo">🎵 PiMusic</div>
        <div id="status-badge" class="status-badge"><div class="dot"></div> <span id="status-text">Disconnected</span></div>
    </div>

    <div class="container">
        <!-- Left Column -->
        <div class="left-col">
            <div class="player-card">
                <div class="album-art-container">
                    <img src="https://via.placeholder.com/300" id="np-img" class="album-art" onerror="this.src='https://via.placeholder.com/300?text=Music'">
                </div>
                <div class="track-info">
                    <div class="track-title" id="np-title">Nothing Playing</div>
                    <div class="track-meta" id="np-meta">--:--</div>
                </div>
                <div class="controls">
                    <button class="btn-ctrl btn-auto" id="btn-auto" onclick="control('autoplay')" title="Autoplay">♾️</button>
                    <button class="btn-ctrl" onclick="control('shuffle')" title="Shuffle">🔀</button>
                    <button class="btn-ctrl btn-play" onclick="control('pause')">⏯</button>
                    <button class="btn-ctrl" onclick="control('skip')" title="Skip">⏭</button>
                </div>
            </div>

            <div class="search-wrap">
                <input type="text" class="search-input" id="urlInput" placeholder="Paste YouTube URL or search..." onkeypress="handleEnter(event)">
                <button class="search-btn" onclick="searchSong()">🔍</button>
            </div>
            <div id="loading" class="spinner"></div>
            <div id="search-results"></div>
        </div>

        <!-- Right Column -->
        <div class="right-col">
            <div class="tabs">
                <button class="tab-btn active" onclick="switchTab('queue')">Queue <span id="q-count">(0)</span></button>
                <button class="tab-btn" onclick="switchTab('playlists')">Playlists</button>
            </div>

            <div id="tab-queue" class="section active">
                <div id="queue-list"></div>
            </div>

            <div id="tab-playlists" class="section">
                <div class="pl-tools">
                    <div class="pl-inputs">
                        <input type="text" class="pl-input" id="plName" placeholder="New Playlist Name">
                        <input type="text" class="pl-input" id="plUrl" placeholder="YouTube URL (Optional)">
                    </div>
                    <button class="btn-save" onclick="savePlaylist()">💾 Save Playlist</button>
                </div>
                <div id="playlist-list"></div>
            </div>
        </div>
    </div>

    <script>
        // State
        let currentTab = 'queue';
        let lastStatus = null;

        function switchTab(tab) {
            currentTab = tab;
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
                
                // Update Header
                const badge = document.getElementById('status-badge');
                if (data.guild) {
                    badge.classList.add('online');
                    document.getElementById('status-text').innerText = data.guild;
                } else {
                    badge.classList.remove('online');
                    document.getElementById('status-text').innerText = "Offline";
                }

                // Update Player
                document.getElementById('np-title').innerText = data.current ? data.current.title : "Nothing Playing";
                document.getElementById('np-meta').innerText = data.current ? `${data.current.author} • ${data.current.duration}` : "Ready to play";
                
                const img = document.getElementById('np-img');
                const newSrc = data.current && data.current.thumbnail ? data.current.thumbnail : 'https://via.placeholder.com/300?text=Music';
                if(img.src !== newSrc && newSrc) img.src = newSrc;

                const autoBtn = document.getElementById('btn-auto');
                if (data.autoplay) autoBtn.classList.add('active'); else autoBtn.classList.remove('active');

                // Update Queue
                document.getElementById('q-count').innerText = `(${data.queue.length})`;
                
                // Only redraw queue if it changed to avoid flicker
                const newQHash = JSON.stringify(data.queue);
                if (lastStatus !== newQHash) {
                    lastStatus = newQHash;
                    const qList = document.getElementById('queue-list');
                    qList.innerHTML = '';
                    
                    if(data.queue.length === 0) {
                        qList.innerHTML = '<div style="text-align:center; color:var(--text-muted); padding:40px;">Queue is empty</div>';
                    } else {
                        data.queue.forEach((track, index) => {
                            const div = document.createElement('div');
                            div.className = 'list-item';
                            if (track.suggested) div.style.opacity = '0.8';
                            
                            const thumb = track.thumbnail ? track.thumbnail : 'https://via.placeholder.com/40';
                            const badge = track.suggested ? '<span class="tag-auto">Auto</span>' : '';
                            
                            let buttons = `<button class="btn-del" onclick="removeTrack(${index})">✕</button>`;
                            if (track.suggested) {
                                buttons = `<button class="btn-action" onclick="regenerateSuggestion()" title="Regenerate">🎲</button>` + buttons;
                            }
                            
                            div.innerHTML = `
                                <div class="item-info">
                                    <img src="${thumb}" class="list-thumb">
                                    <div class="item-text">
                                        <div class="item-title">${index + 1}. ${track.title}${badge}</div>
                                        <div class="item-sub">Next Up</div>
                                    </div>
                                </div>
                                <div style="display:flex;">${buttons}</div>
                            `;
                            qList.appendChild(div);
                        });
                    }
                }

            } catch (e) { document.getElementById('status-badge').classList.remove('online'); }
        }

        async function fetchPlaylists() {
            try {
                const res = await fetch('/api/playlists');
                const data = await res.json();
                const list = document.getElementById('playlist-list');
                list.innerHTML = '';
                if(data.length === 0) { list.innerHTML = '<div style="text-align:center; color:var(--text-muted);">No saved playlists</div>'; return; }
                
                data.forEach(pl => {
                    const icon = pl.type === 'live' ? '🔗' : '💾';
                    const sub = pl.type === 'live' ? 'YouTube Playlist' : `${pl.count} songs`;
                    const div = document.createElement('div');
                    div.className = 'list-item';
                    div.innerHTML = `
                        <div class="item-info">
                            <div style="font-size:1.5rem; margin-right:15px; width:45px; text-align:center;">${icon}</div>
                            <div class="item-text">
                                <div class="item-title">${pl.name}</div>
                                <div class="item-sub">${sub}</div>
                            </div>
                        </div>
                        <div style="display:flex; gap:5px;">
                            <button class="btn-action" onclick="loadPlaylist(this, '${pl.name}')">▶</button>
                            <button class="btn-del" onclick="deletePlaylist('${pl.name}')">🗑</button>
                        </div>
                    `;
                    list.appendChild(div);
                });
            } catch (e) {}
        }

        async function searchSong() {
            const input = document.getElementById('urlInput');
            const resDiv = document.getElementById('search-results');
            const loader = document.getElementById('loading');
            
            if (!input.value.trim()) return;
            
            // Direct play logic for URLs
            if (input.value.includes('http')) { 
                addDirect(input.value); 
                return; 
            }

            resDiv.style.display = 'none'; 
            resDiv.innerHTML = ''; 
            loader.style.display = 'block';

            try {
                const res = await fetch('/api/search', { 
                    method: 'POST', 
                    headers: {'Content-Type': 'application/json'}, 
                    body: JSON.stringify({query: input.value}) 
                });
                const results = await res.json();
                loader.style.display = 'none';
                
                if (results.error) { alert(results.error); return; }
                
                results.forEach(item => {
                    const div = document.createElement('div');
                    div.className = 'res-item';
                    div.onclick = () => { 
                        addDirect(item.url); 
                        resDiv.style.display='none'; 
                        document.getElementById('urlInput').value = ''; 
                    };
                    div.innerHTML = `
                        <img src="${item.thumbnail}" class="res-img" onerror="this.src='https://via.placeholder.com/50'">
                        <div class="item-info">
                            <div class="item-text">
                                <div class="item-title">${item.title}</div>
                                <div class="item-sub">${item.author} • ${item.duration}</div>
                            </div>
                        </div>
                        <div style="color:var(--primary); font-weight:bold; font-size:1.2rem;">+</div>
                    `;
                    resDiv.appendChild(div);
                });
                resDiv.style.display = 'block';
            } catch (e) { 
                loader.style.display = 'none'; 
                alert("Search failed"); 
            }
        }

        async function addDirect(url) {
            document.getElementById('urlInput').value = '';
            // Show optimistic feedback if you wanted, but status update will handle it
            await fetch('/api/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query: url}) });
            fetchStatus();
        }

        async function regenerateSuggestion() { await fetch('/api/control/regenerate', { method: 'POST' }); fetchStatus(); }
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

# --- Auth Helpers ---
def get_bot_cog():
    """Reliably retrieves the MusicBot cog instance."""
    if 'BOT_COG' in app.config:
        return app.config['BOT_COG']
    # Fallback to global search
    if bot_instance:
        return bot_instance.get_cog('MusicBot')
    return None

def get_first_available_guild():
    """Returns the first guild the bot is connected to (for single-server setups)."""
    cog = get_bot_cog()
    if cog and cog.bot:
        if cog.bot.voice_clients:
            return cog.bot.voice_clients[0].guild
        if cog.bot.guilds:
            return cog.bot.guilds[0]
    return None

def get_bot_token():
    """Retrieves the secure web token from the bot instance."""
    cog = get_bot_cog()
    if cog:
        return cog.web_auth_token
    return None

# --- Routes ---

@app.before_request
def check_auth():
    if request.path.startswith('/auth') or request.path.startswith('/cache'):
        return
    
    user_token = request.cookies.get('pi_music_auth')
    server_token = get_bot_token()
    
    if not server_token or user_token != server_token:
        html = """
            <body style="background:#0f0f0f; color:#eee; font-family:sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; margin:0;">
                <h1 style="color:#ff4444; font-size:4rem; margin:0;">NO ACCESS</h1>
                <h2 style="margin-top:10px;">Access Denied</h2>
                <p style="color:#888;">Use <code>/link</code> in Discord to generate a secure key.</p>
            </body>
        """
        return render_template_string(html), 403

@app.route('/auth')
async def auth_route():
    token_from_url = request.args.get('token')
    server_token = get_bot_token()
    
    if token_from_url == server_token:
        resp = await make_response(redirect('/'))
        # Using Lax SameSite policy to allow cookie to set during redirect
        resp.set_cookie('pi_music_auth', token_from_url, max_age=86400, samesite='Lax')
        return resp
    
    log_error(f"Auth Failed. URL Token: {token_from_url}, Server Token: {server_token}")
    return "❌ Invalid Token.", 403

@app.route('/cache/thumb/<path:filename>')
async def serve_thumbnail(filename):
    return await send_from_directory(CACHE_DIR, filename)

@app.route('/')
async def home():
    return await render_template_string(DASHBOARD_HTML)

# --- API Routes ---

@app.route('/api/status')
async def api_status():
    guild = get_first_available_guild()
    cog = get_bot_cog()
    
    # Debug info
    if not cog: log_error("API Error: Bot Cog not found in config.")
    elif not guild: log_error(f"API Error: Bot is online but no guild found. Guilds len: {len(cog.bot.guilds) if cog and cog.bot else 'None'}")
    
    if not guild or not cog:
        return jsonify({'current': None, 'queue': [], 'guild': None, 'autoplay': False})
    
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
            'thumbnail': get_thumbnail_url(t['id']),
            'suggested': t.get('suggested', False)
        })
        
    return jsonify({'current': current, 'queue': queue_data, 'guild': guild.name, 'autoplay': state.autoplay})

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
    
    if not name:
        return jsonify({'error': 'No name'}), 400
        
    if url:
        if 'youtube.com' not in url and 'youtu.be' not in url:
             return jsonify({'error': 'Invalid YouTube URL'}), 400
             
        saved_playlists[name] = {'type': 'live', 'url': url}
        save_json(PLAYLIST_FILE, saved_playlists)
        return jsonify({'status': 'ok'})
    
    # Save current queue
    guild = get_first_available_guild()
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
        
    state = cog.get_state(guild.id)
    
    tracks = []
    if state.current_track:
        tracks.append(state.current_track)
    tracks.extend(state.queue)
    
    if not tracks:
        return jsonify({'error': 'Empty'}), 400
    
    clean = [{
        'id': t['id'], 
        'title': t['title'], 
        'author': t['author'], 
        'duration': t['duration'], 
        'duration_seconds': t['duration_seconds'], 
        'webpage': t['webpage']
    } for t in tracks]
    
    saved_playlists[name] = clean
    save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

@app.route('/api/playlists/load', methods=['POST'])
async def api_load_playlist():
    data = await request.get_json()
    name = data.get('name', '').lower()
    
    if name not in saved_playlists:
        return jsonify({'error': 'Not found'}), 404
        
    guild = get_first_available_guild()
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
        
    state = cog.get_state(guild.id)
    content = saved_playlists[name]
    new_tracks = []
    
    if isinstance(content, list):
        new_tracks = content
    elif isinstance(content, dict):
        try:
            info = await cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAYLIST_LOAD_OPTS).extract_info(content['url'], download=False))
            if 'entries' in info:
                for e in info['entries']:
                    if e:
                        new_tracks.append({
                            'id': e['id'], 
                            'title': e['title'], 
                            'author': e['uploader'], 
                            'duration': format_time(e['duration']), 
                            'duration_seconds': e['duration'], 
                            'webpage': f"https://www.youtube.com/watch?v={e['id']}"
                        })
            asyncio.create_task(cog.load_rest_of_playlist(content['url'], guild.id))
        except Exception as e:
            log_error(f"Playlist load error: {e}")
            return jsonify({'error': 'Fetch fail'}), 500
        
    if new_tracks:
        state.queue.extend(new_tracks)
        # Try to connect if not in VC
        if not guild.voice_client:
            for channel in guild.voice_channels:
                if len(channel.members) > 0:
                    await channel.connect()
                    break

        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v):
                     self.guild = g
                     self.voice_client = v
                     self.author = "WebUser"
                 async def send(self, *args, **kwargs): pass 
             
             await cog.play_next(DummyCtx(guild, guild.voice_client))
        return jsonify({'status': 'ok'})
        
    return jsonify({'error': 'Empty'}), 400

@app.route('/api/playlists/delete', methods=['POST'])
async def api_del_playlist():
    data = await request.get_json()
    if data['name'] in saved_playlists:
        del saved_playlists[data['name']]
        save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

@app.route('/api/search', methods=['POST'])
async def api_search():
    data = await request.get_json()
    cog = get_bot_cog()
    if not cog: return jsonify([]), 500
    try:
        info = await cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{data['query']}", download=False))
        res = []
        if 'entries' in info:
            for e in info['entries']:
                if e:
                    thumb = e.get('thumbnail')
                    if not thumb or not thumb.startswith('http'):
                        thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
                    
                    res.append({
                        'title': e['title'], 
                        'author': e['uploader'], 
                        'duration': format_time(e['duration']), 
                        'url': f"https://www.youtube.com/watch?v={e['id']}", 
                        'thumbnail': thumb
                    })
        return jsonify(res)
    except Exception:
        return jsonify([]), 500

@app.route('/api/control/<action>', methods=['POST'])
async def api_control(action):
    guild = get_first_available_guild()
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
        
    vc = guild.voice_client
    state = cog.get_state(guild.id)
    
    if action == 'pause' and vc: 
        if vc.is_playing():
            vc.pause() 
        elif vc.is_paused():
            vc.resume()
    elif action == 'skip' and vc:
        vc.stop()
    elif action == 'shuffle':
        random.shuffle(state.queue)
    elif action == 'autoplay':
        state.autoplay = not state.autoplay
        await cog.ensure_autoplay(guild.id)
        if state.autoplay and state.queue and vc and not vc.is_playing():
             class DummyCtx:
                 def __init__(self, g, v):
                     self.guild = g
                     self.voice_client = v
                     self.author = "WebUser"
                 async def send(self, *args, **kwargs): pass 
             await cog.play_next(DummyCtx(guild, vc))
    elif action == 'regenerate':
        await cog.regenerate_autoplay(guild.id)
        
    return jsonify({'status':'ok'})

@app.route('/api/remove/<int:index>', methods=['POST'])
async def api_remove(index):
    guild = get_first_available_guild()
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
    
    state = cog.get_state(guild.id)
    if 0 <= index < len(state.queue):
        del state.queue[index]
    return jsonify({'status': 'ok'})

@app.route('/api/add', methods=['POST'])
async def api_add():
    data = await request.get_json()
    guild = get_first_available_guild()
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
    
    state = cog.get_state(guild.id)
    query = data['query']
    if not re.match(r'^https?://', query):
        query = f"ytsearch1:{query}"
    
    if not state.last_text_channel:
        state.last_text_channel = guild.text_channels[0]
    
    # Clear suggestions so user song plays next
    state.queue = [t for t in state.queue if not t.get('suggested')]

    try:
        # Try to connect if not in VC
        if not guild.voice_client:
            for channel in guild.voice_channels:
                if len(channel.members) > 0:
                    await channel.connect()
                    break

        # Use Flat Options (verified working)
        info = await cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
        
        def process(e): 
            url = e.get('webpage_url') or e.get('url') or f"https://www.youtube.com/watch?v={e['id']}"
            return {
                'id': e['id'], 
                'title': e['title'], 
                'author': e.get('uploader', 'Unknown'), 
                'duration': format_time(e.get('duration', 0)), 
                'duration_seconds': e.get('duration', 0), 
                'webpage': url
            }
        
        if 'entries' in info:
            state.queue.append(process(info['entries'][0]))
        else:
            state.queue.append(process(info))
        
        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v):
                     self.guild = g
                     self.voice_client = v
                     self.author = "WebUser"
                 async def send(self, *args, **kwargs): pass 
             
             await cog.play_next(DummyCtx(guild, guild.voice_client))
        else:
             cog.bot.loop.create_task(cog.ensure_autoplay(guild.id))

        return jsonify({'status':'ok'})
    except Exception:
        return jsonify({'error':'fail'}), 500

# ==========================================
# 5. DISCORD UI CLASSES
# ==========================================

class ServerState:
    """Stores the music state for a single guild."""
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
    """Dropdown menu for search results."""
    def __init__(self, entries, cog, ctx):
        options = []
        for entry in entries[:10]:
            title = (entry.get('title', 'Unknown')[:90] + '..') if len(entry.get('title', '')) > 90 else entry.get('title', 'Unknown')
            val = f"https://www.youtube.com/watch?v={entry['id']}" if entry.get('id') else entry.get('url')
            if val: options.append(discord.SelectOption(label=title, value=val))
        super().__init__(placeholder="Select a song...", options=options)
        self.cog, self.ctx = cog, ctx
    async def callback(self, interaction):
        if interaction.user != self.ctx.author: return
        await interaction.response.edit_message(content="✅ **Confirmed.**", view=None)
        await self.cog.prepare_song(self.ctx, self.values[0])

class SelectionView(ui.View):
    """View container for the selection menu."""
    def __init__(self, entries, cog, ctx):
        super().__init__(timeout=30)
        self.add_item(SelectionMenu(entries, cog, ctx))
        self.message = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(content="⌛ **Search expired.**", view=self)
        except: pass

class MusicControlView(ui.View):
    """Persistent buttons for the 'Now Playing' message."""
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
        await interaction.response.send_message("🔀 Shuffled queue!", ephemeral=True, silent=True)
    @ui.button(emoji="📋", style=discord.ButtonStyle.gray)
    async def q_btn(self, interaction, button):
        state = self.cog.get_state(self.guild_id)
        if not state.current_track and not state.queue:
            return await interaction.response.send_message("Queue empty!", ephemeral=True, silent=True)
        view = ListPaginator(state.queue, title="Server Queue", is_queue=True, current=state.current_track)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True, silent=True)
    @ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction, button):
        await self.cog.stop_logic(self.guild_id)
        await interaction.response.send_message("👋 Stopping & Saving...", ephemeral=True, silent=True)

class ListPaginator(ui.View):
    """Pagination for queue, history, and cache lists."""
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
                if isinstance(s, dict): 
                    prefix = "✨ " if s.get('suggested') else ""
                    line = f"`{start+i+1}.` {prefix}**{s['title']}** ({s.get('duration', '?:??')})"
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
        self.tunnel_proc = None
        
        # Store direct reference for reliable access in Quart
        app.config['BOT_COG'] = self
        
        global bot_instance
        bot_instance = bot 
        self.web_task = self.bot.loop.create_task(app.run_task(host='0.0.0.0', port=5000))

    async def cog_unload(self):
        self.cleanup_loop.stop()
        if self.tunnel_proc:
            try: self.tunnel_proc.terminate()
            except: pass
        if self.web_task: self.web_task.cancel()

    # --- Cloudflare Tunnel Logic ---
    def ensure_cloudflared(self):
        """Downloads the correct cloudflared binary for the system."""
        if os.path.exists("./cloudflared"): return True
        
        arch = platform.machine().lower()
        if arch in ['x86_64', 'amd64']: c_arch = 'amd64'
        elif arch in ['aarch64', 'arm64']: c_arch = 'arm64'
        else: c_arch = 'arm' # Pi Zero / Pi 2 / Pi 3 32-bit
        
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{c_arch}"
        log_info(f"⬇️ Downloading Cloudflared ({c_arch})...")
        
        try:
            r = requests.get(url, stream=True)
            with open("./cloudflared", 'wb') as f:
                shutil.copyfileobj(r.raw, f)
            st = os.stat("./cloudflared")
            os.chmod("./cloudflared", st.st_mode | stat.S_IEXEC)
            log_info("✅ Cloudflared installed.")
            return True
        except Exception as e:
            log_error(f"❌ Failed to download cloudflared: {e}")
            return False

    async def start_cloudflared(self):
        """Starts the tunnel and retrieves the URL."""
        if self.public_url: return self.public_url
        
        # Download in background thread to avoid blocking heartbeat
        if not await self.bot.loop.run_in_executor(None, self.ensure_cloudflared):
            return None
        
        # Kill existing
        try: subprocess.run(["pkill", "-f", "cloudflared tunnel"], capture_output=True)
        except: pass

        # Use 127.0.0.1 to avoid IPv6/localhost resolution issues
        self.tunnel_proc = subprocess.Popen(
            ["./cloudflared", "tunnel", "--url", "http://127.0.0.1:5000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Scrape URL from stderr (Cloudflared logs to stderr)
        start_time = time.time()
        while time.time() - start_time < 20:
            # Check if process crashed
            if self.tunnel_proc.poll() is not None:
                err = self.tunnel_proc.stderr.read()
                log_error(f"❌ Cloudflared crashed: {err}")
                return None

            line = self.tunnel_proc.stderr.readline()
            if not line:
                await asyncio.sleep(0.1)
                continue

            if "trycloudflare.com" in line:
                match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
                if match:
                    self.public_url = match.group(0)
                    log_info(f"🌍 Tunnel Active: {self.public_url}")
                    return self.public_url
            await asyncio.sleep(0.1)
        return None

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if hasattr(ctx.command, 'on_error'):
            return
            
        if isinstance(error, commands.CommandNotFound):
            return
            
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Permission denied", silent=True)
        else:
            try:
                await ctx.send(f"❌ Error: {str(error)[:100]}", silent=True)
            except Exception:
                pass

    def get_state(self, guild_id):
        if guild_id not in self.states:
            self.states[guild_id] = ServerState()
        return self.states[guild_id]

    # --- Playback Logic ---

    async def download_session_songs(self, tracks):
        """Background task to cache songs played in the session."""
        if not tracks:
            return
            
        to_download = [t for t in tracks if not os.path.exists(f"{CACHE_DIR}/{t['id']}.webm")]
        if not to_download:
            return
        
        await enforce_cache_limit(self.bot.loop)
        for track in to_download:
            await enforce_cache_limit(self.bot.loop)
            try:
                await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_DOWNLOAD_OPTS).download([f'https://www.youtube.com/watch?v={track["id"]}']))
                cache_map[track['id']] = track['title']
                await self.bot.loop.run_in_executor(None, save_json, CACHE_MAP_FILE, cache_map)
            except Exception as e:
                log_error(f"DL Fail: {e}")
            await asyncio.sleep(0.5)

    async def stop_logic(self, guild_id):
        """Clean disconnect logic."""
        if guild_id not in self.states:
            return
            
        guild = self.bot.get_guild(guild_id)
        state = self.states[guild_id]
        state.stopping = True
        
        if guild and guild.voice_client:
            await guild.voice_client.disconnect()
            
        all_tracks = state.session_new_tracks.copy()
        for t in state.queue:
            all_tracks[t['id']] = t
            
        if all_tracks:
            self.bot.loop.create_task(self.download_session_songs(list(all_tracks.values())))
            
        del self.states[guild_id]

    @tasks.loop(minutes=2)
    async def cleanup_loop(self):
        """Auto-disconnect if alone or idle."""
        now = datetime.datetime.now()
        for gid in list(self.states.keys()):
            guild = self.bot.get_guild(gid)
            if not guild:
                del self.states[gid]
                continue
                
            state = self.states[gid]
            if guild.voice_client:
                # FIX: Reset the timer while music is playing
                if guild.voice_client.is_playing():
                    state.last_interaction = now

                is_alone = len(guild.voice_client.channel.members) == 1
                is_idle = not guild.voice_client.is_playing() and (now - state.last_interaction).total_seconds() > 300
                
                if is_alone or is_idle:
                    await self.stop_logic(gid)

    def get_notification_channel(self, guild):
        if str(guild.id) in server_settings:
            ch_id = server_settings[str(guild.id)]
            ch = guild.get_channel(ch_id)
            if ch and ch.permissions_for(guild.me).send_messages:
                return ch
                
        state = self.get_state(guild.id)
        if state.last_text_channel:
            return state.last_text_channel
            
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                if any(x in ch.name.lower() for x in ['music', 'muzica', 'bot', 'general']):
                    return ch
                    
        return guild.text_channels[0] if guild.text_channels else None

    async def load_rest_of_playlist(self, url, guild_id):
        """Background task to load large playlists."""
        REST_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '51-', **COMMON_YDL_ARGS, 'noplaylist': False}
        try:
            info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(REST_OPTS).extract_info(url, download=False))
            if 'entries' in info:
                state = self.get_state(guild_id)
                count = 0
                for e in info['entries']:
                    if e: 
                        state.queue.append({
                            'id': e['id'], 
                            'title': e['title'], 
                            'author': e['uploader'], 
                            'duration': format_time(e['duration']), 
                            'duration_seconds': e['duration'], 
                            'webpage': f"https://www.youtube.com/watch?v={e['id']}"
                        })
                        count += 1
                
                guild = self.bot.get_guild(guild_id)
                ch = self.get_notification_channel(guild)
                if ch:
                    await ch.send(f"✅ Loaded {count} more tracks in background.", silent=True)
        except Exception:
            pass

    async def ensure_autoplay(self, guild_id, avoid_ids=None):
        """Logic for buffering the next suggested song."""
        state = self.get_state(guild_id)
        if avoid_ids is None: avoid_ids = []
        
        # 1. If Autoplay is OFF, remove any suggested tracks
        if not state.autoplay:
            state.queue = [t for t in state.queue if not t.get('suggested')]
            return

        # 2. If we already have a suggestion at the end, do nothing (unless forced via avoid_ids)
        if not avoid_ids and state.queue and state.queue[-1].get('suggested'):
            return

        # 3. Find a seed track (last in queue, or current)
        seed = None
        for t in reversed(state.queue):
            if not t.get('suggested'): 
                seed = t
                break
        
        if not seed: seed = state.current_track
        if not seed and state.history: seed = state.history[-1]
        
        if not seed: return

        # 4. Fetch recommendation
        try:
            # Run in executor to avoid blocking
            info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_MIX_OPTS).extract_info(f"https://www.youtube.com/watch?v={seed['id']}&list=RD{seed['id']}", download=False))
            if 'entries' in info:
                # History check (last 20)
                recent_ids = [h['id'] for h in state.history[-20:]]
                
                # Filter candidates
                candidates = []
                for e in info['entries']:
                    if not e: continue
                    eid = e['id']
                    if eid == seed['id']: continue
                    if eid in avoid_ids: continue
                    if eid in recent_ids: continue
                    
                    candidates.append(e)
                    if len(candidates) >= 5: break # Get top 5 valid candidates
                
                if candidates:
                    # Pick random from top 5 for variety
                    e = random.choice(candidates)
                    track = {'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':e['url'], 'suggested': True}
                    state.queue.append(track)
                    
        except Exception as e:
            log_error(f"Autoplay fetch failed: {e}")

    async def regenerate_autoplay(self, guild_id):
        """Regenerates the current autoplay suggestion."""
        state = self.get_state(guild_id)
        if not state.autoplay: return False
        
        # Find current suggestion
        if state.queue and state.queue[-1].get('suggested'):
            old_suggestion = state.queue.pop() # Remove it
            # Avoid this one, and also ensure we don't pick it again immediately
            await self.ensure_autoplay(guild_id, avoid_ids=[old_suggestion['id']])
            return True
        else:
            # No suggestion present, just ensure one
            await self.ensure_autoplay(guild_id)
            return True

    async def prepare_song(self, ctx, query):
        """Main entry point for adding a song to the queue."""
        state = self.get_state(ctx.guild.id)
        state.last_interaction = datetime.datetime.now()
        state.stopping = False
        if hasattr(ctx, 'channel'): state.last_text_channel = ctx.channel
        
        # Clear suggestions so user song plays next
        state.queue = [t for t in state.queue if not t.get('suggested')]
        
        # VC Join Logic
        if not ctx.voice_client:
            if ctx.author.voice: 
                try: await ctx.author.voice.channel.connect()
                except Exception as e: return await ctx.send(embed=discord.Embed(description=f"❌ Error joining VC: {e}", color=discord.Color.red()), silent=True)
            else: return await ctx.send(embed=discord.Embed(description="❌ You must be in a Voice Channel!", color=discord.Color.red()), silent=True)

        if ctx.interaction: await ctx.interaction.response.defer()
        
        # Use Flat Options (verified working)
        info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
        
        def proc(e): 
            url = e.get('webpage_url') or e.get('url') or f"https://www.youtube.com/watch?v={e['id']}"
            return {
                'id': e['id'], 
                'title': e['title'], 
                'author': e.get('uploader', 'Unknown'), 
                'duration': format_time(e.get('duration', 0)), 
                'duration_seconds': e.get('duration', 0), 
                'webpage': url
            }
        
        async def send_res(msg):
            if ctx.interaction: await ctx.interaction.followup.send(embed=discord.Embed(description=msg, color=COLOR_MAIN), silent=True)
            else: await ctx.send(embed=discord.Embed(description=msg, color=COLOR_MAIN), silent=True)

        if 'entries' in info: 
            state.queue.extend([proc(e) for e in info['entries'] if e])
            await send_res(f"✅ Added **{len(info['entries'])}** tracks.")
        else: 
            state.queue.append(proc(info))
            if ctx.voice_client.is_playing(): await send_res(f"✅ Queued: **{info['title']}**")
            
        if not ctx.voice_client.is_playing(): await self.play_next(ctx)
        else: 
            # If playing, ensure we have an autoplay queued after this new one
            self.bot.loop.create_task(self.ensure_autoplay(ctx.guild.id))

    async def play_next(self, ctx):
        """Recursive function to play the next song in the queue."""
        state = self.get_state(ctx.guild.id)
        
        if state.stopping or not ctx.guild.voice_client or not ctx.guild.voice_client.is_connected():
             return
        if state.processing_next: return
        
        if state.queue:
            state.processing_next = True 
            next_song = state.queue.pop(0)
            state.current_track = next_song
            state.history.append(next_song)
            if len(state.history) > 20: state.history.pop(0)

            try:
                local = os.path.abspath(f"{CACHE_DIR}/{next_song['id']}.webm")
                play_local = os.path.exists(local) and os.path.getsize(local) > 1024
                
                # Thumbnail Check
                thumb_local = f"{CACHE_DIR}/{next_song['id']}.jpg"
                if play_local and not os.path.exists(thumb_local):
                    try: await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL({'writethumbnail':True, 'skip_download':True, 'outtmpl': f'{CACHE_DIR}/%(id)s.%(ext)s', 'quiet':True}).download([f"https://www.youtube.com/watch?v={next_song['id']}"])) # noqa
                    except: pass

                if play_local:
                    os.utime(local, None)
                    source = await discord.FFmpegOpusAudio.from_probe(local, **FFMPEG_LOCAL_OPTS)
                else:
                    state.session_new_tracks[next_song['id']] = next_song
                    info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAY_OPTS).extract_info(next_song['id'], download=False))
                    
                    opts = FFMPEG_STREAM_OPTS.copy()
                    if 'http_headers' in info:
                        header_args = ""
                        for key, value in info['http_headers'].items():
                            header_args += f"{key}: {value}\r\n"
                        opts['before_options'] = f'-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -headers "{header_args}" -nostdin'
                    
                    source = await discord.FFmpegOpusAudio.from_probe(info['url'], **opts)

                ctx.voice_client.play(source, after=lambda e: self.bot.loop.create_task(self.play_next(ctx)))
                state.processing_next = False 
                
                # Trigger autoplay prefetch for the NEXT song
                self.bot.loop.create_task(self.ensure_autoplay(ctx.guild.id))
                
                embed = discord.Embed(title="🎶 Now Playing", description=f"**[{next_song['title']}]({next_song['webpage']})**", color=COLOR_MAIN)
                embed.set_thumbnail(url=f"https://i.ytimg.com/vi/{next_song['id']}/mqdefault.jpg")
                embed.add_field(name="Author", value=next_song['author'])
                embed.add_field(name="Duration", value=next_song['duration'])
                if next_song.get('suggested'): embed.set_footer(text="✨ Autoplay Suggestion")
                
                ch = self.get_notification_channel(ctx.guild)
                if ch: await ch.send(embed=embed, view=MusicControlView(self, ctx.guild.id), silent=True)
            
            except Exception as e: 
                log_error(f"Playback error: {e}")
                state.processing_next = False
                await asyncio.sleep(2) 
                self.bot.loop.create_task(self.play_next(ctx))
        else:
            state.current_track = None
            state.processing_next = False

    # --- COMMANDS ---
    @commands.hybrid_command(name="help", description="Show all commands")
    async def help(self, ctx):
        embed = discord.Embed(title="🎵 PiMusic Bot Commands", description="Control your music with these commands:", color=COLOR_MAIN)
        embed.add_field(name="🎵 Music", value="`/play [song/url]` - Play music\n`/pause` / `/resume`\n`/skip`\n`/stop`\n`/autoplay`", inline=False)
        embed.add_field(name="🎛️ Dashboard", value="`/link` - Get Web Panel\n`/setchannel` - Set output channel", inline=False)
        embed.add_field(name="📂 Playlists", value="`/saveplaylist`\n`/loadplaylist`\n`/listplaylists`\n`/delplaylist`", inline=False)
        embed.add_field(name="📜 Queue", value="`/queue`\n`/history`\n`/shuffle`", inline=False)
        embed.add_field(name="⚙️ Utils", value="`/search`\n`/cache`\n`/dash`", inline=False)
        await ctx.send(embed=embed, silent=True)

    @commands.command()
    async def sync(self, ctx):
        await ctx.bot.tree.sync()
        await ctx.send("✅ Synced! Commands will appear shortly.", silent=True)

    @commands.hybrid_command(name="setchannel")
    async def set_channel(self, ctx):
        server_settings[str(ctx.guild.id)] = ctx.channel.id
        save_json(SETTINGS_FILE, server_settings)
        embed = discord.Embed(description=f"✅ Bound to {ctx.channel.mention}", color=COLOR_MAIN)
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="link")
    async def link(self, ctx):
        await ctx.defer()
        
        # Auto-join VC
        if not ctx.voice_client and ctx.author.voice:
            try: await ctx.author.voice.channel.connect()
            except: pass

        if not self.public_url:
             self.public_url = await self.start_cloudflared()
        
        if self.public_url:
            secure_link = f"{self.public_url}/auth?token={self.web_auth_token}"
            embed = discord.Embed(title="🎛️ Web Dashboard", description="Click below to open the control panel.", color=COLOR_MAIN)
            embed.set_footer(text="Powered by Cloudflare Tunnel ☁️")
            view = ui.View()
            view.add_item(ui.Button(label="Open Dashboard", url=secure_link))
            await ctx.send(embed=embed, view=view, silent=True)
        else:
            await ctx.send("❌ Could not start Cloudflare Tunnel. Check logs.", silent=True)

    @commands.hybrid_command(name="play", aliases=["p"])
    async def play(self, ctx, *, search: str):
        q = search if re.match(r'^https?://', search) else f"ytsearch1:{search}"
        await self.prepare_song(ctx, q)

    @commands.hybrid_command(name="stop", aliases=["dc", "leave"])
    async def stop(self, ctx): 
        await self.stop_logic(ctx.guild.id)
        embed = discord.Embed(description="👋 Stopped.", color=COLOR_MAIN)
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx): 
        ctx.voice_client.stop()
        embed = discord.Embed(description="⏭️ Skipped.", color=COLOR_MAIN)
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="queue", aliases=["q"])
    async def queue(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.current_track and not state.queue:
            return await ctx.send(embed=discord.Embed(description="Queue empty.", color=COLOR_MAIN), silent=True)
        view = ListPaginator(state.queue, title="Server Queue", is_queue=True, current=state.current_track)
        await ctx.send(embed=view.get_embed(), view=view, silent=True)

    @commands.hybrid_command(name="pause")
    async def pause(self, ctx):
        if ctx.voice_client.is_playing(): 
            ctx.voice_client.pause()
            await ctx.send(embed=discord.Embed(description="⏸️ Paused.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="resume")
    async def resume(self, ctx):
        if ctx.voice_client.is_paused(): 
            ctx.voice_client.resume()
            await ctx.send(embed=discord.Embed(description="▶️ Resumed.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="shuffle")
    async def shuffle(self, ctx):
        random.shuffle(self.get_state(ctx.guild.id).queue)
        await ctx.send(embed=discord.Embed(description="🔀 Shuffled.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="saveplaylist")
    async def saveplaylist(self, ctx, name: str, url: str = None):
        if url: 
            # Added validation from Working
            if 'youtube.com' not in url and 'youtu.be' not in url:
                return await ctx.send(embed=discord.Embed(description="❌ Invalid YouTube URL.", color=discord.Color.red()), silent=True)
            saved_playlists[name] = {'type': 'live', 'url': url}
        else:
            state = self.get_state(ctx.guild.id)
            tracks = []
            if state.current_track: tracks.append(state.current_track)
            tracks.extend(state.queue)
            if not tracks: return await ctx.send(embed=discord.Embed(description="Queue empty.", color=discord.Color.red()), silent=True)
            clean = [{'id':t['id'], 'title':t['title'], 'author':t['author'], 'duration':t['duration'], 'duration_seconds':t['duration_seconds'], 'webpage':t['webpage']} for t in tracks]
            saved_playlists[name] = clean
        save_json(PLAYLIST_FILE, saved_playlists)
        await ctx.send(embed=discord.Embed(description=f"💾 Saved **{name}**.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="loadplaylist")
    async def loadplaylist(self, ctx, name: str):
        if name not in saved_playlists: return await ctx.send(embed=discord.Embed(description="❌ Not found.", color=discord.Color.red()), silent=True)
        content = saved_playlists[name]
        state = self.get_state(ctx.guild.id)
        
        if isinstance(content, list):
            state.queue.extend(content)
            await ctx.send(embed=discord.Embed(description=f"📂 Loaded **{len(content)}** songs.", color=COLOR_MAIN), silent=True)
        elif isinstance(content, dict):
            await ctx.send(embed=discord.Embed(description="🔄 Loading live playlist (First 50)...", color=COLOR_MAIN), silent=True)
            try:
                info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAYLIST_LOAD_OPTS).extract_info(content['url'], download=False))
                tracks = [{'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':f"https://www.youtube.com/watch?v={e['id']}"} for e in info['entries'] if e]
                state.queue.extend(tracks)
                await ctx.send(embed=discord.Embed(description=f"✅ Loaded **{len(tracks)}**. Rest loading in BG...", color=COLOR_MAIN), silent=True)
                asyncio.create_task(self.load_rest_of_playlist(content['url'], ctx.guild.id))
            except: await ctx.send(embed=discord.Embed(description="❌ Error loading.", color=discord.Color.red()), silent=True)

        if not ctx.voice_client:
            if ctx.author.voice: await ctx.author.voice.channel.connect()
        if not ctx.voice_client.is_playing(): await self.play_next(ctx)

    @commands.hybrid_command(name="listplaylists")
    async def listplaylists(self, ctx):
        msg = "\n".join([f"{k}" for k in saved_playlists.keys()])
        await ctx.send(embed=discord.Embed(title="📂 Saved Playlists", description=msg if msg else "None", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="delplaylist")
    async def delplaylist(self, ctx, name: str):
        if name in saved_playlists: 
            del saved_playlists[name]
            save_json(PLAYLIST_FILE, saved_playlists)
            await ctx.send(embed=discord.Embed(description=f"🗑️ Deleted **{name}**.", color=COLOR_MAIN), silent=True)
        else: await ctx.send(embed=discord.Embed(description="❌ Not found.", color=discord.Color.red()), silent=True)

    @commands.hybrid_command(name="cache")
    async def cache_list(self, ctx):
        valid = [f for f in os.listdir(CACHE_DIR) if f.endswith('.webm')]
        data = [{'title': cache_map.get(f.replace('.webm',''), f), 'duration': 'Cached'} for f in valid]
        if not data: return await ctx.send(embed=discord.Embed(description="Cache empty.", color=COLOR_MAIN), silent=True)
        data.sort(key=lambda x: x['title'])
        view = ListPaginator(data, title="Local Cache", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view, silent=True)

    @commands.hybrid_command(name="dash")
    async def dash(self, ctx):
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        try: temp = os.popen("vcgencmd measure_temp").readline().replace("temp=","").strip()
        except: temp = "N/A"
        count = len([n for n in os.listdir(CACHE_DIR) if n.endswith('.webm')])
        size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in os.listdir(CACHE_DIR) if f.endswith('.webm')) / (1024**3)
        embed = discord.Embed(title="🚀 Pi Stats", color=COLOR_MAIN)
        embed.add_field(name="System", value=f"CPU: `{cpu}%` | RAM: `{ram}%` | {temp}")
        embed.add_field(name="Storage", value=f"`{count}` songs | `{size:.2f} GB` / {MAX_CACHE_SIZE_GB} GB")
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="search")
    async def search(self, ctx, *, query: str):
        await ctx.defer()
        info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{query}", download=False))
        if not info.get('entries'): return await ctx.send("❌ No results.", silent=True)
        view = SelectionView(info['entries'], self, ctx)
        view.message = await ctx.send("🔎 **Results:**", view=view, silent=True)

    @commands.hybrid_command(name="history")
    async def history(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.history: return await ctx.send(embed=discord.Embed(description="History empty.", color=COLOR_MAIN), silent=True)
        view = ListPaginator(list(reversed(state.history)), title="History", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view, silent=True)

    @commands.hybrid_command(name="autoplay")
    async def autoplay(self, ctx):
        state = self.get_state(ctx.guild.id)
        state.autoplay = not state.autoplay
        await self.ensure_autoplay(ctx.guild.id)
        
        if state.autoplay and state.queue and ctx.guild.voice_client and not ctx.guild.voice_client.is_playing():
             await self.play_next(ctx)

        await ctx.send(embed=discord.Embed(description=f"📻 Auto-Play: **{'ON' if state.autoplay else 'OFF'}**", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="new", aliases=["regen", "mix"], description="Regenerate the autoplay suggestion")
    async def new_suggestion(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.autoplay:
            return await ctx.send(embed=discord.Embed(description="❌ Auto-Play is OFF.", color=discord.Color.red()), silent=True)
        
        await ctx.defer()
        if await self.regenerate_autoplay(ctx.guild.id):
            await ctx.send(embed=discord.Embed(description="🎲 **Regenerated suggestion!**", color=COLOR_MAIN), silent=True)
        else:
            await ctx.send(embed=discord.Embed(description="❌ Could not regenerate.", color=discord.Color.red()), silent=True)

# ==========================================
# 7. STARTUP
# ==========================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True  # CRITICAL FIX for API visibility

bot = commands.Bot(command_prefix='$', intents=intents)
bot.remove_command('help') 

@bot.event
async def on_ready():
    log_info(f'✅ Logged in as {bot.user}')
    global bot_instance
    bot_instance = bot 
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="$help"))
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
        log_info("👋 Bot Shutdown.")

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        log_info("🚀 UVLoop enabled.")
    except ImportError:
        pass
    asyncio.run(main())
