# Multi-Detect G20 custom QGroundControl build.

message(STATUS "QGC: Adding Multi-Detect custom plugin")

list(APPEND QGC_RESOURCES ${CMAKE_CURRENT_SOURCE_DIR}/custom.qrc)
set(QGC_RESOURCES
    ${QGC_RESOURCES}
    CACHE STRING "Paths to custom QGC resources" FORCE
)

set(MULTIDETECT_STATE_QML res/Custom/MultiDetect/MultiDetectState.qml)
set(MULTIDETECT_OVERLAY_QML res/Custom/MultiDetect/MultiDetectVideoOverlay.qml)
set(MULTIDETECT_FIXED_WING_HUD_QML res/Custom/MultiDetect/FixedWingVideoHud.qml)

set_source_files_properties(${MULTIDETECT_STATE_QML} PROPERTIES QT_QML_SINGLETON_TYPE TRUE)

qt_add_library(MultiDetectUi STATIC)
qt_add_qml_module(
    MultiDetectUi
    URI Custom.MultiDetect
    VERSION 1.0
    RESOURCE_PREFIX /qml
    QML_FILES
        ${MULTIDETECT_STATE_QML}
        ${MULTIDETECT_OVERLAY_QML}
        ${MULTIDETECT_FIXED_WING_HUD_QML}
    NO_PLUGIN
)

add_library(
    MultiDetectOperatorProtocol STATIC
    src/MultiDetectDepthGridProtocol.cc
    src/MultiDetectDepthGridProtocol.h
    src/MultiDetectDepthGridReceiver.cc
    src/MultiDetectDepthGridReceiver.h
    src/MultiDetectOperatorProtocol.cc
    src/MultiDetectOperatorProtocol.h
)
target_link_libraries(MultiDetectOperatorProtocol PUBLIC Qt6::Core Qt6::Network)
target_include_directories(MultiDetectOperatorProtocol PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)

add_executable(MultiDetectOperatorProtocolSelfTest EXCLUDE_FROM_ALL tests/MultiDetectOperatorProtocolSelfTest.cc)
target_link_libraries(MultiDetectOperatorProtocolSelfTest PRIVATE MultiDetectOperatorProtocol Qt6::Core)

add_executable(
    MultiDetectDepthGridProtocolSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectDepthGridProtocolSelfTest.cc
)
target_link_libraries(MultiDetectDepthGridProtocolSelfTest PRIVATE MultiDetectOperatorProtocol Qt6::Core)

# These uppercase cache names are the public QGC custom-build integration API.
# cmake-lint: disable=C0103
set(CUSTOM_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/src/CustomPlugin.cc ${CMAKE_CURRENT_SOURCE_DIR}/src/CustomPlugin.h
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectOperatorController.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectOperatorController.h
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectTargetEvidenceStore.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectTargetEvidenceStore.h
    CACHE INTERNAL "" FORCE
)

set(CUSTOM_LIBRARIES
    MultiDetectUi MultiDetectOperatorProtocol
    CACHE INTERNAL "" FORCE
)

set(CUSTOM_INCLUDE_DIRECTORIES
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    CACHE INTERNAL "" FORCE
)

set(CUSTOM_DEFINITIONS
    QGC_CUSTOM_BUILD CUSTOMHEADER="CustomPlugin.h" CUSTOMCLASS=CustomPlugin
    CACHE INTERNAL "" FORCE
)

set(CUSTOM_QT_COMPONENTS
    Core
    Gui
    Network
    CACHE INTERNAL "" FORCE
)
