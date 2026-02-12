import os
import signal
import subprocess
import hashlib
import time
import shutil
import logging
from flask import Flask, Response, request, render_template_string, send_from_directory, abort

# --- 配置区域 ---
# Render/Zeabur 会自动注入 PORT 环境变量，本地默认 8080
PORT = int(os.environ.get('PORT', 8080))
HLS_DIR = "hls_streams"
FFMPEG_BIN = "ffmpeg"

# 初始化 Flask
app = Flask(__name__)
# 减少日志噪音
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 存储活跃的转码进程
# 结构: {stream_id: {"process": subprocess.Popen, "last_access": time.time(), "source": url}}
active_streams = {}

# 启动时清理旧数据
if os.path.exists(HLS_DIR):
    shutil.rmtree(HLS_DIR)
os.makedirs(HLS_DIR)

# HTML 播放器模板 (支持移动端和PC)
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
        .video-wrapper { margin-top: 20px; border-radius: 8px; overflow: hidden; }
        .status { margin-top: 15px; font-size: 0.85em; color: #aaa; word-break: break-all; background: #25252b; padding: 10px; border-radius: 4px;}
        a { color: #6c5ce7; }
    </style>
</head>
<body>
    <div class="container">
        <h2>RTP 直播流转码网关</h2>
        <form action="/play" method="get">
            <input type="text" name="url" placeholder="输入 RTP/UDP 地址 (如 http://IP:PORT/rtp/...)" value="{{ source_url }}" required>
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
        <script src="https://cdnjs.cloudflare.com/ajax/libs/video.js/7.20.3/video.min.js"></script>
        {% endif %}
    </div>
</body>
</html>
"""

def get_stream_id(url):
    """根据URL生成唯一的ID"""
    return hashlib.md5(url.encode('utf-8')).hexdigest()

def clean_stale_streams():
    """清理超过60秒没有被访问的流，防止云服务器资源耗尽"""
    now = time.time()
    to_remove = []
    for sid, data in active_streams.items():
        if now - data["last_access"] > 60:  # 60秒超时
            logger.info(f"Stream {sid} timed out, stopping...")
            try:
                data["process"].terminate()
                data["process"].wait(timeout=2)
            except:
                data["process"].kill()
            to_remove.append(sid)
            # 清理文件
            shutil.rmtree(os.path.join(HLS_DIR, sid), ignore_errors=True)
    
    for sid in to_remove:
        del active_streams[sid]

def start_ffmpeg(source_url, stream_id):
    clean_stale_streams() # 启动新流前尝试清理旧流
    
    output_dir = os.path.join(HLS_DIR, stream_id)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    playlist_path = os.path.join(output_dir, "index.m3u8")
    
    # FFmpeg 命令优化：
    # -c copy: 直接复制流，不消耗CPU进行转码
    # -hls_time 2: 切片更小，延迟更低
    # -hls_list_size 4: 列表只保留4个切片
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i", source_url,
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "4",
        "-hls_flags", "delete_segments+omit_endlist",
        "-hls_segment_filename", os.path.join(output_dir, "%03d.ts"),
        playlist_path
    ]

    logger.info(f"Starting FFmpeg: {source_url} -> {stream_id}")
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
    
    # 检查进程是否存在且存活
    if stream_id in active_streams:
        if active_streams[stream_id]["process"].poll() is not None:
            del active_streams[stream_id] # 进程已死，移除
        else:
            active_streams[stream_id]["last_access"] = time.time() # 更新活跃时间
    
    # 如果未运行，启动它
    if stream_id not in active_streams:
        start_ffmpeg(source_url, stream_id)
        # 等待 FFmpeg 生成第一个切片，避免 404
        retries = 20
        while retries > 0:
            if os.path.exists(os.path.join(HLS_DIR, stream_id, "index.m3u8")):
                break
            time.sleep(0.2)
            retries -= 1

    # 构造外部可访问的 URL
    # 处理 HTTPS (Render/Zeabur 外部是 HTTPS，内部是 HTTP)
    scheme = "https" if request.headers.get('X-Forwarded-Proto') == 'https' else "http"
    m3u8_url = f"{scheme}://{request.host}/hls/{stream_id}/index.m3u8"
    
    return render_template_string(PLAYER_TEMPLATE, source_url=source_url, m3u8_url=m3u8_url)

@app.route('/hls/<stream_id>/<filename>')
def serve_hls(stream_id, filename):
    # 只要有请求拉取切片，就认为该流是活跃的
    if stream_id in active_streams:
        active_streams[stream_id]["last_access"] = time.time()
    
    try:
        response = send_from_directory(os.path.join(HLS_DIR, stream_id), filename)
        # 允许跨域，方便嵌入其他网页
        response.headers['Access-Control-Allow-Origin'] = '*'
        # 禁用缓存，保证直播实时性
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response
    except FileNotFoundError:
        return abort(404)

if __name__ == '__main__':
    # 监听 0.0.0.0 是 Docker 容器被外部访问的关键
    app.run(host='0.0.0.0', port=PORT, threaded=True)
