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
SDP

# mDNS: UxPlay, Avahi'ye D-Bus üzerinden bağlanır (avahi-client).
# 1) HA OS'in kendi Avahi'si (host_dbus), 2) olmazsa konteyner içinde dbus + avahi.
avahi_ok() {
  dbus-send --system --print-reply --dest=org.freedesktop.Avahi / \
    org.freedesktop.Avahi.Server.GetVersionString >/dev/null 2>&1
}

if avahi_ok; then
  echo "[run] Ana makinenin Avahi servisi kullanılıyor (host_dbus)"
else
  echo "[run] Ana makinede Avahi bulunamadı, konteyner içinde başlatılıyor"
  mkdir -p /run/avahi-daemon /etc/avahi
  cat > /tmp/own-bus.conf <<BUS
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <listen>unix:path=/tmp/uxplay-bus</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
  </policy>
</busconfig>
BUS
  rm -f /tmp/uxplay-bus
  dbus-daemon --config-file=/tmp/own-bus.conf --fork
  export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/tmp/uxplay-bus
  cat > /etc/avahi/avahi-daemon.conf <<AVAHI
[server]
host-name=uxplay-tv
use-ipv4=yes
use-ipv6=no
enable-dbus=yes
[publish]
publish-hinfo=no
publish-workstation=no
[rlimits]
AVAHI
  avahi-daemon --no-drop-root --no-chroot --no-rlimits -D || echo "[run] avahi-daemon başlatılamadı"
  for _ in $(seq 1 20); do avahi_ok && break; sleep 0.5; done
  avahi_ok && echo "[run] Konteyner içi Avahi hazır" || echo "[run] UYARI: Avahi'ye ulaşılamıyor, AirPlay cihazı görünmeyebilir"
fi

exec python3 /opt/uxplay-tv/streamer.py
