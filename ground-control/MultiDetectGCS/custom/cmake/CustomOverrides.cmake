# Keep this custom build focused on the Pixhawk V6X/PX4 aircraft used by Multi-Detect.
set(QGC_APP_NAME
    "MultiDetectGCS"
    CACHE STRING "CMake target and executable name" FORCE
)
set(QGC_APP_VERSION_OVERRIDE
    "0.2.0"
    CACHE STRING "Multi-Detect product version" FORCE
)
set(QGC_WINDOWS_INSTALLER_FILENAME
    "MultiDetectGCS-v0.2.0-windows-amd64.exe"
    CACHE STRING "Versioned Windows installer filename" FORCE
)
set(QGC_APP_DESCRIPTION
    "Multi-Detect Ground Control Station"
    CACHE STRING "Application description" FORCE
)
set(QGC_PACKAGE_NAME
    "com.multidetect.gcs"
    CACHE STRING "Unique Multi-Detect package identifier" FORCE
)
set(QGC_ANDROID_PACKAGE_NAME
    "com.multidetect.gcs"
    CACHE STRING "Unique Multi-Detect Android package identifier" FORCE
)

set(QGC_DISABLE_APM_MAVLINK
    ON
    CACHE BOOL "Disable the unused APM dialect" FORCE
)
set(QGC_DISABLE_APM_PLUGIN
    ON
    CACHE BOOL "Disable the unused APM plugin" FORCE
)
set(QGC_DISABLE_APM_PLUGIN_FACTORY
    ON
    CACHE BOOL "Disable the unused APM factory" FORCE
)

# QGC owns signed target selection and confirmation. Jetson is the single owner
# of Mode-3 Pixhawk attitude setpoints, so direct duplicate writes stay absent here.
add_compile_definitions(MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES=0 MULTIDETECT_PHYSICAL_RELEASE=0)
