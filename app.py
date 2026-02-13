import os
import signal
import subprocess
import hashlib
import time
import shutil
import logging
from flask import Flask, Response, request, render_template_string, send_from_directory, abort

# --- 配置区域 ---
PORT = int(os.environ.get('PORT', 8080))
HLS_DIR = "hls_streams"
FFMPEG_BIN = "ffmpeg"

# 初始化 Flask
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 存储活跃的转码进程
active_streams = {}

# 启动时清理旧数据
if os.path.exists(HLS_DIR):
    shutil.rmtree(HLS_DIR)
os.makedirs(HLS_DIR)

# HTML 播放器模板 (增加了错误处理提示)
PLAYER_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>IPTV Stream Gateway</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/video.js/7.20.3/video-js.min.css" rel="stylesheet" />
    <style>
        body { font-family: sans-serif; background: #0f0f13; color: #eee; margin: 0; padding: 20px; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
        .container { width: 100%; max-width: 800px; background: #1e1e24; padding: 20px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }
        h2 { margin-top: 0; color: #fff; text-align: center; }
        input { width: 100%; padding: 12px; margin-bottom: 10px; border-radius: 6px; border: 1px solid #333; background: #2b2b36; color: white; box-sizing: border-box;}
        button { width: 100%; padding: 12px; cursor: pointer; background: #6c5ce7; color: white; border: none; border-radius: 6px; font-weight: bold; }
        button:hover { background: #5b4bc4; }
        .video-wrapper { margin-top: 20px; border-radius: 8px; overflow: hidden; position: relative; min-height: 200px; background: #000;}
        .status { margin-top: 15px; font-size: 0.85em; color: #aaa; word-break: break-all; background: #25252b; padding: 10px; border-radius: 4px;}
        .hint { color: #f39c12; font-size: 0.8em; margin-top: 5px; text-align: left;}
        a { color: #6c5ce7; }
    </style>
</head>
<body>
    <div class="container">
        <h2>RTP 直播流转码网关 (优化版)</h2>
        <form action="/play" method="get">
            <input type="text" name="url" placeholder="输入 RTP/UDP 地址" value="{{ source_url }}" required>
            <button type="submit">开始播放 / 转换</button>
        </form>
        
        {% if m3u8_url %}
        <div class="video-wrapper">
            <video id="my-video" class="video-js vjs-default-skin vjs-big-play-centered" controls preload="auto" width="640" height="360" data-setup='{"fluid": true, "liveui": true}'>
                <source src="{{ m3u8_url }}" type="application/x-mpegURL">
            </video>
        </div>
        <div class="status">
            <strong>M3U8 链接:</strong> <br>
            <a href="{{ m3u8_url }}" target="_blank">{{ m3u8_url }}</a>
        </div>
        <div class="hint">
            <strong>注意：</strong>
            <ul>
                <li>如果画面黑屏但有声音：说明源是 H.265 编码，请复制 m3u8 链接使用 PotPlayer 或 VLC 播放。</li>
                <li>如果加载慢：已增加缓冲时间以保证流畅度，请耐心等待 10-15 秒。</li>
            </ul>
        </div>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/video.js/7.20.3/video.min.js"></script>
        {% endif %}
    </div>
</body>
</html>
"""

def get_stream_id(url):
    return hashlib.md5(url.encode('utf-8')).hexdigest()

def clean_stale_streams():
    now = time.time()
    to_remove = []
    for sid, data in active_streams.items():
        if now - data["last_access"] > 90:  # 延长超时时间到 90秒
            logger.info(f"Stream {sid} timed out, stopping...")
            try:
                data["process"].terminate()
                data["process"].wait(timeout=2)
            except:
                data["process"].kill()
            to_remove.append(sid)
            shutil.rmtree(os.path.join(HLS_DIR, sid), ignore_errors=True)
    
    for sid in to_remove:
        del active_streams[sid]

def start_ffmpeg(source_url, stream_id):
    clean_stale_streams()
    
    output_dir = os.path.join(HLS_DIR, stream_id)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    playlist_path = os.path.join(output_dir, "index.m3u8")
    
    # --- 核心优化配置 ---
    cmd = [
        FFMPEG_BIN,
        "-y",
        
        # 1. 输入流分析优化
        "-fflags", "+genpts+discardcorrupt", # 丢弃损坏的包，重新生成时间戳
        "-analyzeduration", "5000000",       # 分析 5秒 (提高识别率)
        "-probesize", "5000000",             # 探测 5MB 数据
        "-timeout", "5000000",               # 网络超时 5秒
        
        "-i", source_url,
        
        # 2. 视频处理 (保持复制，否则 CPU 爆炸)
        "-c:v", "copy",
        
        # 3. 音频处理 (关键优化：转码为 AAC)
        # 解决源是 AC3/EAC3 导致浏览器无声的问题
        "-c:a", "aac", 
        "-b:a", "128k", 
        "-ac", "2",      # 强制双声道
        
        # 4. HLS 切片优化 (以延迟换流畅)
        "-f", "hls",
        "-hls_time", "5",         # 切片改为 5秒 (原2秒) -> 更抗抖动
        "-hls_list_size", "6",    # 列表保留 6个切片 (30秒缓冲)
        "-hls_flags", "delete_segments+omit_endlist+split_by_time",
        "-hls_segment_filename", os.path.join(output_dir, "%03d.ts"),
        
        playlist_path
    ]

    logger.info(f"Starting FFmpeg (Optimized): {source_url} -> {stream_id}")
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    active_streams[stream_id] = {
        "process": process,
        "last_access": time.time(),
        "source": source_url
    }

@app.route('/')
def index():
    return render_template_string(PLAYER_TEMPLATE, source_url="", m3u8_url=None)

@app.route('/play')
def play():
    source_url = request.args.get('url')
    if not source_url:
        return "Missing URL", 400
    
    stream_id = get_stream_id(source_url)
    
    if stream_id in active_streams:
        if active_streams[stream_id]["process"].poll() is not None:
            del active_streams[stream_id]
        else:
            active_streams[stream_id]["last_access"] = time.time()
    
    if stream_id not in active_streams:
        start_ffmpeg(source_url, stream_id)
        # 增加等待时间，因为切片变大了
        retries = 30 
        while retries > 0:
            if os.path.exists(os.path.join(HLS_DIR, stream_id, "index.m3u8")):
                # 再次检查文件大小，确保不是空文件
                if os.path.getsize(os.path.join(HLS_DIR, stream_id, "index.m3u8")) > 0:
                    break
            time.sleep(0.5)
            retries -= 1

    scheme = "https" if request.headers.get('X-Forwarded-Proto') == 'https' else "http"
    m3u8_url = f"{scheme}://{request.host}/hls/{stream_id}/index.m3u8"
    
    return render_template_string(PLAYER_TEMPLATE, source_url=source_url, m3u8_url=m3u8_url)

@app.route('/hls/<stream_id>/<filename>')
def serve_hls(stream_id, filename):
    if stream_id in active_streams:
        active_streams[stream_id]["last_access"] = time.time()
    
    try:
        response = send_from_directory(os.path.join(HLS_DIR, stream_id), filename)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response
    except FileNotFoundError:
        return abort(404)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, threaded=True)
