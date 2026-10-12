#!/usr/bin/env python3
"""UxPlay'den gelen RTP (H.264 + L16 ses) akışını MPEG-TS'ye çevirip HTTP ile sunar.

Bir iPhone yayını başlayınca (ilk TS verisi gelince) HA'daki TV'ye play_media gönderir,
yayın bitince (idle_timeout kadar veri gelmezse) TV'yi durdurur.
"""
import json
import os
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SDP_FILE = os.environ.get("SDP_FILE", "/tmp/uxplay.sdp")
PORT = int(os.environ.get("STREAM_PORT") or 8090)
CONTENT_TYPE = os.environ.get("MEDIA_CONTENT_TYPE") or "video/mpeg"
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE") or "128k"
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT") or 20)
TV_ENTITY = os.environ.get("TV_ENTITY", "")
AUTO_PLAY = os.environ.get("AUTO_PLAY", "true") == "true"
STOP_ON_END = os.environ.get("STOP_TV_ON_END", "true") == "true"
DEBUG = os.environ.get("DEBUG", "false") == "true"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")


def log(*a):
    print("[streamer]", *a, file=sys.stderr, flush=True)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


STREAM_HOST = os.environ.get("STREAM_HOST") or local_ip()
STREAM_URL = f"http://{STREAM_HOST}:{PORT}/stream.ts"

clients = set()
clients_lock = threading.Lock()
state = {"live": False}


def broadcast(data):
    with clients_lock:
        for q in list(clients):
            try:
                q.put_nowait(data)
            except queue.Full:  # yavaş istemcide gecikmeyi sınırla: eskiyi at
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass


def close_clients():
    with clients_lock:
        for q in list(clients):
            try:
                q.put_nowait(None)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass


def ha_call(method, path, payload=None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log("SUPERVISOR_TOKEN yok, HA çağrısı atlandı")
        return None
    req = urllib.request.Request(
        "http://supervisor/core/api/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return json.loads(body) if body else None
    except Exception as e:  # noqa: BLE001
        log("HA çağrısı başarısız:", path, e)
        return None


def on_start():
    log("Yayın başladı ->", STREAM_URL)
    state["live"] = True
    if AUTO_PLAY and TV_ENTITY:
        ha_call("POST", "services/media_player/play_media", {
            "entity_id": TV_ENTITY,
            "media_content_id": STREAM_URL,
            "media_content_type": CONTENT_TYPE,
        })


def on_end():
    log("Yayın bitti")
    state["live"] = False
    if STOP_ON_END and TV_ENTITY:
        st = ha_call("GET", f"states/{TV_ENTITY}")
        attrs = (st or {}).get("attributes", {})
        if attrs.get("media_content_id") in (STREAM_URL, None):
            ha_call("POST", "services/media_player/media_stop", {"entity_id": TV_ENTITY})


def ffmpeg_cmd():
    return [
        FFMPEG, "-hide_banner", "-loglevel", "info" if DEBUG else "warning",
        "-protocol_whitelist", "file,udp,rtp",
        "-fflags", "+nobuffer+genpts", "-flags", "low_delay",
        "-analyzeduration", "1000000", "-probesize", "500000",
        "-use_wallclock_as_timestamps", "1",
        "-i", SDP_FILE,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy", "-bsf:v", "dump_extra=freq=keyframe",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100",
        "-af", "aresample=async=1000",
        "-max_interleave_delta", "300000",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers",
        "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
        "pipe:1",
    ]


def session():
    p = subprocess.Popen(ffmpeg_cmd(), stdout=subprocess.PIPE, bufsize=0)
    fd = p.stdout.fileno()
    started = False
    last = time.time()
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                data = os.read(fd, 65536)
                if not data:
                    break
                if not started:
                    started = True
                    on_start()
                last = time.time()
                broadcast(data)
            elif p.poll() is not None:
                break
            elif started and time.time() - last > IDLE_TIMEOUT:
                log(f"{IDLE_TIMEOUT:.0f} sn veri gelmedi, oturum kapatılıyor")
                break
    finally:
        p.kill()
        p.wait()
        close_clients()
        if started:
            on_end()


def ffmpeg_loop():
    while True:
        session()
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        if DEBUG:
            log(self.address_string(), fmt % args)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            body = json.dumps({"live": state["live"], "url": STREAM_URL}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if not (self.path == "/" or self.path.startswith("/stream.ts")):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org",
                         "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        q = queue.Queue(maxsize=256)
        with clients_lock:
            clients.add(q)
        log("İstemci bağlandı:", self.address_string())
        try:
            while True:
                try:
                    data = q.get(timeout=1)
                except queue.Empty:
                    continue
                if data is None:
                    break
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with clients_lock:
                clients.discard(q)
            log("İstemci ayrıldı:", self.address_string())


def main():
    log("Akış adresi:", STREAM_URL, "| TV:", TV_ENTITY or "(ayarlanmadı)")
    threading.Thread(target=ffmpeg_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
