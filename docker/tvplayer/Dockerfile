# puffpai-tvplayer: the kiosk's hardware-decoded TV player.
#
# Why this image exists: the kiosk's snap Chromium runs with GL off, so Live TV
# was decoded AND scaled to 2560x1440 on the CPU. This image plays a channel in
# its own full-screen window on the kiosk display, decoded by a Jetson's video
# engine (NVIDIA nvv4l2decoder) and drawn by the GPU (nv3dsink).
#
# Base = Ubuntu 22.04 on purpose: JetPack R36 is built on 22.04, so this
# image's GStreamer (1.20) is the same ABI as the host's, and NVIDIA's
# GStreamer plugins and Tegra libraries can be bind-mounted in from the host at
# run time (tv_player.py does that, the same file list the NVIDIA container
# runtime would mount; this box has no nvidia runtime in docker). Nothing from
# NVIDIA is baked into the image.
#
# Inside: tvplay (entry script), tvwindow.py (full-screen window + decode/draw
# pipeline), hlsfetch.py (HLS fetch + decrypt), tsfix.py (keeps stream ids
# steady across ad switches). ffmpeg only
# remuxes (-c copy, no re-encode) to even out timestamp jumps: free
# ad-stitched channels (Pluto and the like) restart their timestamps at every
# ad switch, which Jellyfin's own remux (-copyts) cannot play through. See
# tvplay for the order things run in.
#
# Pinned: base image by digest. Ubuntu package versions follow the jammy
# archive at build time; the versions this image was built with are listed in
# /usr/share/puffpai-tvplayer/packages.txt inside the image.
FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-pulseaudio \
      libegl1 libgles2 libx11-6 libv4lconvert0 \
      ffmpeg curl jq procps kmod ca-certificates \
      python3-gi python3-xlib gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/share/puffpai-tvplayer /opt/nvv4l2 \
 && dpkg-query -W -f '${Package} ${Version}\n' > /usr/share/puffpai-tvplayer/packages.txt \
 # NVIDIA's decoder plugin links libv4l2.so.0 and expects NVIDIA's build of it
 # (the host's /usr/lib/aarch64-linux-gnu/libv4l2.so.0 points at
 # nvidia/libnvv4l2.so). This link resolves once the host lib dir is mounted.
 && ln -s /usr/lib/aarch64-linux-gnu/nvidia/libnvv4l2.so /opt/nvv4l2/libv4l2.so.0
COPY tvplay.sh /usr/local/bin/tvplay
COPY tvwindow.py /usr/local/bin/tvwindow.py
COPY tsfix.py /usr/local/bin/tsfix.py
COPY hlsfetch.py /usr/local/bin/hlsfetch.py
RUN chmod 0755 /usr/local/bin/tvplay /usr/local/bin/tvwindow.py /usr/local/bin/tsfix.py /usr/local/bin/hlsfetch.py
ENV LD_LIBRARY_PATH=/opt/nvv4l2:/usr/lib/aarch64-linux-gnu/nvidia:/usr/lib/aarch64-linux-gnu/tegra-egl \
    GST_PLUGIN_PATH=/opt/nvgst \
    GST_REGISTRY=/tmp/gst-registry.bin
ENTRYPOINT ["/usr/local/bin/tvplay"]
