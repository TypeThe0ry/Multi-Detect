# Legacy GPU graphics base only: does not apply the PX4 scheduler fix.
# Use scripts/build_px4_lockstep_candidate.sh for the patched runtime; the
# source-level GPU default pins that runtime by immutable image ID.
# Optional GPU-rendering variant for the pinned PX4/Gazebo Harmonic image.
# NVIDIA Container Toolkit supplies the host driver libraries at runtime;
# this layer only provides the EGL userspace entry points and diagnostics.
FROM px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8

ENV HEADLESS=1 \
    GZ_SIM_RENDER_ENGINE=ogre2 \
    GZ_SIM_HEADLESS_RENDERING=1 \
    EGL_PLATFORM=surfaceless \
    NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute \
    __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json \
    LIBGL_ALWAYS_SOFTWARE=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libegl1 \
        libglvnd0 \
        mesa-utils-extra \
    && rm -rf /var/lib/apt/lists/*

# The toolkit injects libEGL_nvidia and the matching GLVND vendor manifest
# when the container is started with --gpus.  If a host toolkit uses a
# different manifest path, omit __EGL_VENDOR_LIBRARY_FILENAMES at runtime and
# let GLVND discover its injected vendor files.
