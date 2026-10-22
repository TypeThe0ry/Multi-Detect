FROM px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8

ENV DEBIAN_FRONTEND=noninteractive \
    LIBGL_ALWAYS_SOFTWARE=1 \
    DISPLAY=:99

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        imagemagick mesa-utils novnc websockify x11-apps x11vnc xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY start-viewer.sh /usr/local/bin/start-viewer.sh
RUN chmod 0755 /usr/local/bin/start-viewer.sh
