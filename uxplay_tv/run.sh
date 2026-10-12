#!/bin/bash
# UxPlay (AirPlay alıcısı) + streamer.py (RTP -> MPEG-TS -> HTTP) başlatıcısı
set -e

OPT=/data/options.json
opt() { jq -r --arg k "$1" '.[$k] // empty' "$OPT"; }

export AIRPLAY_NAME="$(opt airplay_name)"
export TV_ENTITY="$(opt tv_entity)"
export STREAM_HOST="$(opt stream_host)"
export STREAM_PORT="$(opt stream_port)"
export MEDIA_CONTENT_TYPE="$(opt media_content_type)"
export AUDIO_BITRATE="$(opt audio_bitrate)"
export IDLE_TIMEOUT="$(opt idle_timeout)"
export AUTO_PLAY="$(opt auto_play)"
export STOP_TV_ON_END="$(opt stop_tv_on_end)"
export DEBUG="$(opt debug)"
RESOLUTION="$(opt resolution)"
FPS="$(opt fps)"

VIDEO_PORT=5000
AUDIO_PORT=5002
export SDP_FILE=/tmp/uxplay.sdp

cat > "$SDP_FILE" <<SDP
v=0
o=- 0 0 IN IP4 127.0.0.1
s=UxPlay
c=IN IP4 127.0.0.1
t=0 0
m=video ${VIDEO_PORT} RTP/AVP 96
a=rtpmap:96 H264/90000
m=audio ${AUDIO_PORT} RTP/AVP 97
a=rtpmap:97 L16/44100/2
SDP

# Kendi mDNS (Avahi) servisimiz: HA OS'in avahi'sine erişimimiz yok.
mkdir -p /run/avahi-daemon /etc/avahi
cat > /etc/avahi/avahi-daemon.conf <<AVAHI
[server]
host-name=uxplay-tv
use-ipv4=yes
use-ipv6=no
enable-dbus=no
[publish]
publish-hinfo=no
publish-workstation=no
[rlimits]
AVAHI
avahi-daemon --no-drop-root --no-chroot --no-rlimits -D
for _ in $(seq 1 20); do [ -S /run/avahi-daemon/socket ] && break; sleep 0.5; done

python3 /opt/uxplay-tv/streamer.py &
STREAMER_PID=$!

UXPLAY_ARGS=(
  -n "$AIRPLAY_NAME" -nh
  -p
  -s "$RESOLUTION" -fps "$FPS"
  -nohold
  -vrtp "pt=96 config-interval=1 ! udpsink host=127.0.0.1 port=${VIDEO_PORT}"
  -artp "pt=97 ! udpsink host=127.0.0.1 port=${AUDIO_PORT}"
)
[ "$DEBUG" = "true" ] && UXPLAY_ARGS+=(-d)

uxplay "${UXPLAY_ARGS[@]}" &
UXPLAY_PID=$!

trap 'kill $STREAMER_PID $UXPLAY_PID 2>/dev/null; exit 0' TERM INT
wait -n $STREAMER_PID $UXPLAY_PID
echo "Bir süreç sonlandı, add-on yeniden başlatılıyor." >&2
kill $STREAMER_PID $UXPLAY_PID 2>/dev/null || true
exit 1
