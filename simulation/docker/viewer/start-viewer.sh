#!/bin/sh
set -eu

: "${VIEWER_PORT:=6080}"
: "${GZ_GUI_RENDER_ENGINE:=ogre2}"
: "${GZ_GUI_START_DELAY:=8}"
export DISPLAY="${DISPLAY:-:99}"

Xvfb "$DISPLAY" -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 -quiet &
websockify --web=/usr/share/novnc "$VIEWER_PORT" 127.0.0.1:5900 &

(
    sleep "$GZ_GUI_START_DELAY"
    if ! DISPLAY="$DISPLAY" LIBGL_ALWAYS_SOFTWARE=1 gz sim -g \
        --render-engine-gui "$GZ_GUI_RENDER_ENGINE" \
        /opt/px4-gazebo/share/gz/worlds/default.sdf >/tmp/gz-gui.log 2>&1; then
        DISPLAY="$DISPLAY" LIBGL_ALWAYS_SOFTWARE=1 gz sim -g \
            --render-engine-gui ogre \
            /opt/px4-gazebo/share/gz/worlds/default.sdf >>/tmp/gz-gui.log 2>&1 || true
    fi
) &

exec "$@"
