FROM debian:bookworm-slim

ARG UXPLAY_VERSION=v1.73.7
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git build-essential cmake pkg-config \
        libssl-dev libplist-dev libavahi-compat-libdnssd-dev \
        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-libav \
        avahi-daemon ffmpeg python3 jq \
    && git clone --depth 1 --branch "${UXPLAY_VERSION}" https://github.com/FDH2/UxPlay.git /tmp/uxplay \
    && cmake -S /tmp/uxplay -B /tmp/uxplay/build -DNO_X11_DEPS=ON -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/uxplay/build -j"$(nproc)" \
    && install -m 0755 /tmp/uxplay/build/uxplay /usr/local/bin/uxplay \
    && rm -rf /tmp/uxplay /var/lib/apt/lists/*

COPY run.sh streamer.py /opt/uxplay-tv/
RUN chmod +x /opt/uxplay-tv/run.sh

CMD ["/opt/uxplay-tv/run.sh"]
