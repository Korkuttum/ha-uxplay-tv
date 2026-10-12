#!/usr/bin/env python3
"""UxPlay'den gelen RTP (H.264 + L16 ses) akışını MPEG-TS'ye çevirip HTTP ile sunar.

Bir iPhone yayını başlayınca (ilk TS verisi gelince) HA'daki TV'ye play_media gönderir,
yayın bitince (idle_timeout kadar veri gelmezse) TV'yi durdurur.
"""
import array
import collections
import json
import os
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from tsmux import H264Depacketizer, TsMuxer
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
HA_API = os.environ.get("HA_API", "http://supervisor/core/api/")
STREAM_PORT = 8090
UI_PORT = int(os.environ.get("UI_PORT") or 8099)
AUDIO_BITRATE = "128k"
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT") or 30)
DEBUG = os.environ.get("DEBUG", "false") == "true"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
UXPLAY = os.environ.get("UXPLAY", "uxplay")
VIDEO_IN_PORT = 5010   # UxPlay -> Python
AUDIO_IN_PORT = 5020   # UxPlay -> Python (RTP L16)
RESOLUTION = "1920x1080"
FPS = "30"
DEFAULTS = {"tv_entity": "", "airplay_name": "", "media_content_type": "video/mpeg"}

events = collections.deque(maxlen=40)


def log(*a):
    msg = " ".join(str(x) for x in a)
    events.append(time.strftime("%H:%M:%S ") + msg)
    print("[streamer]", msg, file=sys.stderr, flush=True)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


STREAM_HOST = local_ip()
STREAM_URL = f"http://{STREAM_HOST}:{STREAM_PORT}/stream.ts"

clients = set()
clients_lock = threading.Lock()
state = {"live": False, "uxplay": False}
settings_lock = threading.Lock()
restart_evt = threading.Event()


def get_settings():
    with settings_lock:
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save_settings(data):
    with settings_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)


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
        HA_API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            if method == "POST":
                log(f"HA çağrısı tamam: {path} (HTTP {r.status})")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        log(f"HA çağrısı başarısız: {path} -> HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
        return None
    except Exception as e:  # noqa: BLE001
        log("HA çağrısı başarısız:", path, e)
        return None


def on_start():
    cfg = get_settings()
    log("Yayın başladı ->", STREAM_URL)
    state["live"] = True
    if cfg["tv_entity"]:
        ha_call("POST", "services/media_player/play_media", {
            "entity_id": cfg["tv_entity"],
            "media_content_id": STREAM_URL,
            "media_content_type": cfg["media_content_type"],
        })


def on_end():
    cfg = get_settings()
    log("Yayın bitti")
    state["live"] = False
    if cfg["tv_entity"]:
        st = ha_call("GET", f"states/{cfg['tv_entity']}")
        attrs = (st or {}).get("attributes", {})
        if attrs.get("media_content_id") in (STREAM_URL, None):
            ha_call("POST", "services/media_player/media_stop", {"entity_id": cfg["tv_entity"]})


audio_lock = threading.Lock()
audio_buf = bytearray()
last_video = [0.0]
BYTES_PER_SEC = 44100 * 4
DELAY90 = 36000  # 0.4 sn oynatma tamponu (90 kHz)
first_video = threading.Event()
teardown_evt = threading.Event()
au_q = queue.Queue(maxsize=900)
cur = {"sess": None, "have_key": False}


def put_au(item):
    try:
        au_q.put_nowait(item)
    except queue.Full:
        try:
            au_q.get_nowait()
            au_q.put_nowait(item)
        except (queue.Empty, queue.Full):
            pass


def rtp_relay():
    """UxPlay'den gelen video RTP'yi kare kare birleştirir, ses RTP'sini PCM tampona alır."""
    vs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    vs.bind(("127.0.0.1", VIDEO_IN_PORT))
    au = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    au.bind(("127.0.0.1", AUDIO_IN_PORT))
    depay = H264Depacketizer()
    while True:
        ready, _, _ = select.select([vs, au], [], [], 1.0)
        for sock in ready:
            data = sock.recv(65535)
            if sock is vs:
                last_video[0] = time.time()
                unit = depay.feed(data)
                if not unit:
                    continue
                _, _, key = unit
                if cur["sess"] is None:
                    # oturum yokken yalnızca son anahtar kareden itibaren tut
                    if key:
                        with au_q.mutex:
                            au_q.queue.clear()
                        cur["have_key"] = True
                        put_au(unit)
                        first_video.set()
                    elif cur["have_key"]:
                        put_au(unit)
                else:
                    put_au(unit)
            elif len(data) > 12:
                payload = data[12 + 4 * (data[0] & 0x0F):]
                pcm = array.array("h")
                pcm.frombytes(payload[: len(payload) // 4 * 4])
                pcm.byteswap()  # L16 ağ sırası (BE) -> s16le
                with audio_lock:
                    audio_buf.extend(pcm.tobytes())
                    extra = len(audio_buf) - int(BYTES_PER_SEC * 0.4)
                    if extra > 0:
                        del audio_buf[: extra // 4 * 4]


def audio_feeder(stdin, stop):
    """ffmpeg'e gerçek zamanlı PCM verir: ses varsa onu, yoksa sessizlik."""
    chunk = BYTES_PER_SEC // 50  # 20 ms
    with audio_lock:
        audio_buf.clear()
    nxt = time.monotonic()
    try:
        while not stop.is_set():
            with audio_lock:
                n = min(len(audio_buf), chunk) // 4 * 4
                data = bytes(audio_buf[:n])
                del audio_buf[:n]
            stdin.write(data + bytes(chunk - n))
            stdin.flush()
            nxt += 0.02
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                nxt = time.monotonic()
    except (BrokenPipeError, OSError, ValueError):
        pass


def audio_cmd():
    """PCM -> AAC (ADTS). Video ffmpeg'e hiç girmez."""
    return [
        FFMPEG, "-hide_banner", "-loglevel", "info" if DEBUG else "error",
        "-thread_queue_size", "512", "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-flush_packets", "1", "-f", "adts", "pipe:1",
    ]


def adts_reader(stream, sess):
    buf = bytearray()
    fd = stream.fileno()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return
        buf += chunk
        while len(buf) >= 7:
            if buf[0] != 0xFF or (buf[1] & 0xF0) != 0xF0:
                del buf[0]
                continue
            flen = ((buf[3] & 3) << 11) | (buf[4] << 3) | (buf[5] >> 5)
            if flen < 7:
                del buf[0]
                continue
            if len(buf) < flen:
                break
            sess.audio_frame(bytes(buf[:flen]))
            del buf[:flen]


class Session:
    """Bir yansıtma oturumu: video + ses -> MPEG-TS -> bağlı istemciler."""

    GOP_MAX_BYTES = 6_000_000

    def __init__(self):
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.mux = TsMuxer(broadcast)
        self.gop = collections.deque()
        self.gop_bytes = 0
        self.v0 = None
        self.a_off = None
        self.a_n = 0
        self.started = False

    def now90(self):
        return int((time.monotonic() - self.t0) * 90000)

    def video(self, au, rtp_ts, key):
        """True dönerse bu çağrıyla yayın başladı."""
        with self.lock:
            if not self.started and not key:
                return False
            now = self.now90()
            if self.v0 is None:
                self.v0 = (rtp_ts, now)
            pts = DELAY90 + self.v0[1] + ((rtp_ts - self.v0[0]) & 0xFFFFFFFF)
            if abs(pts - (DELAY90 + now)) > 180000:  # saat kayması: yeniden hizala
                self.v0 = (rtp_ts, now)
                pts = DELAY90 + now
            if key:
                self.gop.clear()
                self.gop_bytes = 0
            self.gop.append(au)
            self.gop_bytes += len(au)
            if self.gop_bytes > self.GOP_MAX_BYTES:
                self.gop.clear()
                self.gop_bytes = 0
            first = not self.started
            self.started = True
            self.mux.video(au, pts, key)
            return first

    def audio_frame(self, frame):
        with self.lock:
            if not self.started:
                return
            now = self.now90()
            if self.a_off is None:
                self.a_off = now
            pts = DELAY90 + self.a_off + self.a_n * 1024 * 90000 // 44100
            if abs(pts - (DELAY90 + now)) > 90000:
                self.a_off = now - self.a_n * 1024 * 90000 // 44100
                pts = DELAY90 + now
            self.a_n += 1
            self.mux.audio(frame, pts, int((time.monotonic() - self.t0) * 27_000_000))

    def join(self, q):
        """Yeni istemciyi kaydeder; son anahtar karedan başlayan kısa bir giriş verir."""
        with self.lock:
            snap = []
            if self.gop:
                m = TsMuxer(snap.append)
                n = len(self.gop)
                base = max(DELAY90 + self.now90() - (n + 1) * 270, 0)
                for i, au in enumerate(self.gop):
                    m.video(au, base + i * 270, key=(i == 0))
            with clients_lock:
                clients.add(q)
            return b"".join(snap)


def session():
    s = Session()
    p = subprocess.Popen(audio_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    stop = threading.Event()
    threading.Thread(target=audio_feeder, args=(p.stdin, stop), daemon=True).start()
    threading.Thread(target=adts_reader, args=(p.stdout, s), daemon=True).start()
    cur["sess"] = s
    teardown_evt.clear()
    last_video[0] = time.time()
    started = False
    log("oturum açıldı, kareler bekleniyor")
    try:
        while True:
            try:
                au, ts, key = au_q.get(timeout=1)
            except queue.Empty:
                au = None
            if au is not None and s.video(au, ts, key):
                started = True
                threading.Thread(target=on_start, daemon=True).start()
            if teardown_evt.is_set():
                log("iPhone yansıtmayı kapattı")
                break
            if time.time() - last_video[0] > IDLE_TIMEOUT:
                log(f"{IDLE_TIMEOUT:.0f} sn görüntü gelmedi, oturum kapatılıyor")
                break
            if p.poll() is not None:
                log("ses kodlayıcı (ffmpeg) beklenmedik şekilde kapandı")
                break
    finally:
        cur["sess"] = None
        cur["have_key"] = False
        first_video.clear()
        with au_q.mutex:
            au_q.queue.clear()
        stop.set()
        p.kill()
        p.wait()
        close_clients()
        if started:
            on_end()


def ffmpeg_loop():
    while True:
        first_video.wait()  # yalnızca bir anahtar kare geldiğinde oturum aç
        session()
        time.sleep(0.5)


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
        self.send_header("Content-Type", get_settings()["media_content_type"])
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org",
                         "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        q = queue.Queue(maxsize=256)
        sess_now = cur["sess"]
        if sess_now is not None:
            snap = sess_now.join(q)
        else:
            snap = b""
            with clients_lock:
                clients.add(q)
        log("İstemci bağlandı:", self.address_string(), "|", (self.headers.get("User-Agent") or "-")[:60])
        try:
            if snap:
                self.wfile.write(snap)
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


def media_players():
    states = ha_call("GET", "states")
    if states is None:
        return None
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        if eid.startswith("media_player."):
            out.append({
                "entity_id": eid,
                "name": s.get("attributes", {}).get("friendly_name") or eid,
                "state": s.get("state"),
            })
    out.sort(key=lambda x: x["name"].lower())
    return out


def friendly_name(entity_id):
    st = ha_call("GET", f"states/{entity_id}")
    return (st or {}).get("attributes", {}).get("friendly_name") or entity_id


def pump_uxplay(proc):
    """UxPlay çıktısını log'a aktarır; yansıtma kapanınca oturumu bitirir."""
    for line in proc.stdout:
        print(line, end="", file=sys.stderr, flush=True)
        if "video_renderer_stop" in line:
            teardown_evt.set()


def uxplay_manager():
    """Ayarlardaki ada göre UxPlay'i başlatır; ayar değişince yeniden başlatır."""
    while True:
        cfg = get_settings()
        if not cfg["tv_entity"]:
            restart_evt.wait()
            restart_evt.clear()
            continue
        name = cfg["airplay_name"] or friendly_name(cfg["tv_entity"])
        args = [UXPLAY, "-n", name, "-nh", "-p", "-s", RESOLUTION, "-fps", FPS, "-nohold",
                "-vrtp", f"pt=96 config-interval=1 ! udpsink host=127.0.0.1 port={VIDEO_IN_PORT}",
                "-artp", f"pt=97 ! udpsink host=127.0.0.1 port={AUDIO_IN_PORT}"]
        if DEBUG:
            args.append("-d")
        log("UxPlay başlatılıyor, AirPlay adı:", name)
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1)
            threading.Thread(target=pump_uxplay, args=(proc,), daemon=True).start()
        except OSError as e:
            log("UxPlay başlatılamadı:", e)
            restart_evt.wait(10)
            restart_evt.clear()
            continue
        state["uxplay"] = True
        started_at = time.time()
        while proc.poll() is None and not restart_evt.wait(1):
            pass
        if restart_evt.is_set():
            restart_evt.clear()
        elif time.time() - started_at < 10:
            log("UxPlay hemen kapandı (mDNS/Avahi sorunu olabilir), 30 sn sonra yeniden denenecek")
            restart_evt.wait(30)
            restart_evt.clear()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        state["uxplay"] = False
        time.sleep(1)


class UIHandler(BaseHTTPRequestHandler):
    """Home Assistant Ingress paneli (yalnızca Supervisor üzerinden erişilir)."""

    def log_message(self, fmt, *args):
        if DEBUG:
            log("ui", fmt % args)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/index.html"):
            body = UI_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.endswith("/api/entities"):
            players = media_players()
            self._json({"ok": players is not None, "entities": players or []})
        elif path.endswith("/api/logs"):
            self._json({"events": list(events)})
        elif path.endswith("/api/state"):
            self._json({**get_settings(), "live": state["live"], "uxplay": state["uxplay"],
                        "stream_url": STREAM_URL})
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        if not self.path.split("?")[0].endswith("/api/settings"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "geçersiz istek"}, 400)
            return
        entity = str(data.get("tv_entity", "")).strip()
        if not entity.startswith("media_player."):
            self._json({"error": "Lütfen bir medya oynatıcı seçin"}, 400)
            return
        name = str(data.get("airplay_name", "")).strip()[:60] or friendly_name(entity)
        cur = get_settings()
        save_settings({
            "tv_entity": entity,
            "airplay_name": name,
            "media_content_type": str(data.get("media_content_type") or cur["media_content_type"]).strip(),
        })
        restart_evt.set()
        self._json({"ok": True, "airplay_name": name})


UI_HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UxPlay TV</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1b1d21;--mut:#6b7280;--line:#d9dce1;--acc:#03a9f4;--ok:#1b9e4b;--bad:#d03a3a}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1c1f26;--fg:#e8eaed;--mut:#9aa0aa;--line:#2f343d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:520px;margin:0 auto;padding:24px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:16px}
h1{font-size:1.3rem;margin:0 0 4px}p{margin:0 0 12px;color:var(--mut)}
label{display:block;font-weight:600;margin:16px 0 6px}
select,input{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg);font:inherit}
button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:10px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5}
.row{display:flex;align-items:center;gap:8px;margin:6px 0}.dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
.dot.on{background:var(--ok)}.msg{margin-top:12px;min-height:1.4em}.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
details{margin-top:16px}summary{cursor:pointer;color:var(--mut)}code{word-break:break-all}
</style></head><body><main>
<div class="card">
<h1>iPhone → TV ekran yansıtma</h1>
<p>TV'nizi seçin. iPhone'da Ekran Yansıtma listesinde bu isimle görünür.</p>
<label for="ent">TV (medya oynatıcı)</label>
<select id="ent"><option value="">Yükleniyor…</option></select>
<label for="name">AirPlay adı</label>
<input id="name" maxlength="60" placeholder="TV adı">
<details><summary>Gelişmiş</summary>
<label for="mime">Akış MIME türü</label>
<input id="mime" placeholder="video/mpeg">
<p style="margin-top:6px">TV akışı açmazsa <code>video/mp2t</code> deneyin.</p></details>
<button id="save">Kaydet</button>
<div id="msg" class="msg"></div>
</div>
<div class="card">
<div class="row"><span id="d1" class="dot"></span><span id="t1">AirPlay alıcısı</span></div>
<div class="row"><span id="d2" class="dot"></span><span id="t2">Yayın yok</span></div>
<p style="margin-top:10px">Akış adresi: <code id="url"></code></p>
</div>
<div class="card">
<strong>Son olaylar</strong>
<pre id="ev" style="white-space:pre-wrap;font-size:.8rem;color:var(--mut);margin:8px 0 0">—</pre>
</div>
</main>
<script>
const $=id=>document.getElementById(id);let dirty=false,names={};
const BASE=location.pathname.endsWith("/")?location.pathname:location.pathname+"/";
const api=p=>fetch(BASE+"api/"+p);
$("name").addEventListener("input",()=>dirty=true);
$("ent").addEventListener("change",()=>{if(!dirty||!$("name").value)$("name").value=names[$("ent").value]||"";dirty=false});
async function load(){
  const [e,s]=await Promise.all([api("entities").then(r=>r.json()),api("state").then(r=>r.json())]);
  const sel=$("ent");sel.innerHTML="";
  if(!e.ok){sel.innerHTML='<option value="">Home Assistant\'a ulaşılamadı</option>';}
  else{
    sel.add(new Option("— TV seçin —",""));
    e.entities.forEach(x=>{names[x.entity_id]=x.name;sel.add(new Option(x.name+"  ("+x.entity_id+")",x.entity_id))});
  }
  sel.value=s.tv_entity||"";
  $("name").value=s.airplay_name||names[s.tv_entity]||"";
  $("mime").value=s.media_content_type||"";
  $("url").textContent=s.stream_url;
  refresh(s);
}
function refresh(s){
  $("d1").className="dot"+(s.uxplay?" on":"");
  $("t1").textContent=s.uxplay?("AirPlay alıcısı çalışıyor: "+(s.airplay_name||"")):"AirPlay alıcısı kapalı (TV seçip kaydedin)";
  $("d2").className="dot"+(s.live?" on":"");
  $("t2").textContent=s.live?"Yayın sürüyor":"Yayın yok";
}
$("save").onclick=async()=>{
  const b=$("save"),m=$("msg");b.disabled=true;m.className="msg";m.textContent="";
  try{
    const r=await fetch(BASE+"api/settings",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tv_entity:$("ent").value,airplay_name:$("name").value,media_content_type:$("mime").value})});
    const j=await r.json();
    if(!r.ok)throw new Error(j.error||"Kaydedilemedi");
    $("name").value=j.airplay_name;m.className="msg ok";m.textContent="Kaydedildi. iPhone'da “"+j.airplay_name+"” görünecek.";
    setTimeout(poll,2500);
  }catch(x){m.className="msg err";m.textContent=x.message}
  b.disabled=false;
};
async function poll(){try{refresh(await api("state").then(r=>r.json()));
  const l=await api("logs").then(r=>r.json());$("ev").textContent=l.events.length?l.events.slice(-15).join("\n"):"—"}catch(e){}}
load().then(poll).catch(e=>{$("ent").innerHTML='<option value="">Liste yüklenemedi: '+e.message+"</option>"});setInterval(poll,5000);
</script></body></html>"""


def main():
    log("Akış adresi:", STREAM_URL)
    threading.Thread(target=rtp_relay, daemon=True).start()
    threading.Thread(target=ffmpeg_loop, daemon=True).start()
    threading.Thread(target=uxplay_manager, daemon=True).start()
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", UI_PORT), UIHandler).serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", STREAM_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
