#!/bin/bash
# Avahi (mDNS) + SDP dosyasını hazırlar, sonra streamer.py'yi başlatır.
# UxPlay'i streamer.py yönetir (TV ve AirPlay adı ingress panelinden seçilir).
set -e

export DEBUG="$(jq -r '.debug // false' /data/options.json)"
export SDP_FILE=/tmp/uxplay.sdp

cat > "$SDP_FILE" <<SDP
v=0
o=- 0 0 IN IP4 127.0.0.1
s=UxPlay
c=IN IP4 127.0.0.1
t=0 0
m=video 5000 RTP/AVP 96
a=rtpmap:96 H264/90000
m=audio 5002 RTP/AVP 97
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

exec python3 /opt/uxplay-tv/streamer.py
