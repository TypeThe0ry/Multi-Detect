# Multi-Detect G20 custom QGroundControl build.

message(STATUS "QGC: Adding Multi-Detect custom plugin")

list(APPEND QGC_RESOURCES ${CMAKE_CURRENT_SOURCE_DIR}/custom.qrc)
set(QGC_RESOURCES
    ${QGC_RESOURCES}
    CACHE STRING "Paths to custom QGC resources" FORCE
)

set(MULTIDETECT_STATE_QML res/Custom/MultiDetect/MultiDetectState.qml)
set(MULTIDETECT_OVERLAY_QML res/Custom/MultiDetect/MultiDetectVideoOverlay.qml)
set(MULTIDETECT_TOOLBAR_INDICATOR_QML res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml)

set_source_files_properties(${MULTIDETECT_STATE_QML} PROPERTIES QT_QML_SINGLETON_TYPE TRUE)
set_source_files_properties(
    ${MULTIDETECT_TOOLBAR_INDICATOR_QML}
    PROPERTIES QT_RESOURCE_ALIAS MultiDetectToolbarIndicator.qml
)

qt_add_library(MultiDetectUi STATIC)
qt_add_qml_module(
    MultiDetectUi
    URI Custom.MultiDetect
    VERSION 1.0
    RESOURCE_PREFIX /qml
    QML_FILES
        ${MULTIDETECT_STATE_QML}
        ${MULTIDETECT_OVERLAY_QML}
        ${MULTIDETECT_TOOLBAR_INDICATOR_QML}
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
    src/MultiDetectMissionWire.cc
    src/MultiDetectMissionWire.h
    src/MultiDetectMissionPackage.cc
    src/MultiDetectMissionPackage.h
    src/MultiDetectMissionController.cc
    src/MultiDetectMissionController.h
    src/MultiDetectMissionSecurity.cc
    src/MultiDetectMissionSecurity.h
)
target_link_libraries(MultiDetectOperatorProtocol PUBLIC Qt6::Core Qt6::Network)
target_include_directories(MultiDetectOperatorProtocol PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)

# QGC desktop builds already ship an OpenSSL runtime.  Keep the standalone
# protocol target buildable on minimal systems by making the Ed25519 backend
# optional; production/REAL upload fails closed when it is unavailable.
set(MULTIDETECT_OPENSSL_ROOT "" CACHE PATH "OpenSSL root containing lib/cmake/OpenSSL or include + lib")
find_package(OpenSSL QUIET COMPONENTS Crypto
    HINTS "${MULTIDETECT_OPENSSL_ROOT}" "$ENV{OPENSSL_ROOT_DIR}")
# A MinGW/MSYS2 OpenSSL found on PATH is ABI-incompatible with MSVC: its CRT
# headers clash with the MSVC ones and break the build.  Treat it as absent
# (backend unavailable, REAL upload fails closed) and say so.
set(MULTIDETECT_OPENSSL_USABLE OFF)
if(TARGET OpenSSL::Crypto)
    set(MULTIDETECT_OPENSSL_USABLE ON)
    if(MSVC AND OPENSSL_INCLUDE_DIR MATCHES "[Mm][Ss][Yy][Ss]|[Mm][Ii][Nn][Gg][Ww]")
        message(WARNING "Multi-Detect mission security: ignoring MinGW OpenSSL at ${OPENSSL_INCLUDE_DIR} for an MSVC build; set MULTIDETECT_OPENSSL_ROOT to an MSVC OpenSSL to enable Ed25519 signing")
        set(MULTIDETECT_OPENSSL_USABLE OFF)
    endif()
endif()
if(MULTIDETECT_OPENSSL_USABLE)
    message(STATUS "Multi-Detect mission security: using OpenSSL Crypto Ed25519 backend")
    target_compile_definitions(MultiDetectOperatorProtocol PRIVATE MULTIDETECT_HAVE_OPENSSL=1)
    target_link_libraries(MultiDetectOperatorProtocol PRIVATE OpenSSL::Crypto)
    # A shared OpenSSL (e.g. vcpkg x64-windows) must ship next to the portable exe.
    get_target_property(MULTIDETECT_OPENSSL_CRYPTO_TYPE OpenSSL::Crypto TYPE)
    if(WIN32 AND MULTIDETECT_OPENSSL_CRYPTO_TYPE STREQUAL "SHARED_LIBRARY")
        install(IMPORTED_RUNTIME_ARTIFACTS OpenSSL::Crypto RUNTIME DESTINATION bin COMPONENT Runtime)
    elseif(WIN32 AND MULTIDETECT_OPENSSL_ROOT)
        # FindOpenSSL's UNKNOWN target carries no runtime; ship the root's DLL.
        file(GLOB MULTIDETECT_OPENSSL_CRYPTO_DLLS "${MULTIDETECT_OPENSSL_ROOT}/bin/libcrypto-*.dll")
        if(MULTIDETECT_OPENSSL_CRYPTO_DLLS)
            install(FILES ${MULTIDETECT_OPENSSL_CRYPTO_DLLS} DESTINATION bin COMPONENT Runtime)
        endif()
    endif()
endif()

add_executable(MultiDetectOperatorProtocolSelfTest EXCLUDE_FROM_ALL tests/MultiDetectOperatorProtocolSelfTest.cc)
target_link_libraries(MultiDetectOperatorProtocolSelfTest PRIVATE MultiDetectOperatorProtocol Qt6::Core)

add_executable(MultiDetectMissionSelfTest EXCLUDE_FROM_ALL tests/MultiDetectMissionSelfTest.cc)
target_link_libraries(MultiDetectMissionSelfTest PRIVATE MultiDetectOperatorProtocol Qt6::Core)

add_executable(MultiDetectMissionSecuritySelfTest EXCLUDE_FROM_ALL tests/MultiDetectMissionSecuritySelfTest.cc)
target_link_libraries(MultiDetectMissionSecuritySelfTest PRIVATE MultiDetectOperatorProtocol Qt6::Core)

add_executable(
    MultiDetectTargetEvidenceStoreSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectTargetEvidenceStoreSelfTest.cc
    src/MultiDetectTargetEvidenceStore.cc
    src/MultiDetectTargetEvidenceStore.h
)
target_include_directories(MultiDetectTargetEvidenceStoreSelfTest PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(MultiDetectTargetEvidenceStoreSelfTest PRIVATE Qt6::Core)

add_library(
    MultiDetectTargetEventReplayStore STATIC
    src/MultiDetectTargetEventReplayStore.cc
    src/MultiDetectTargetEventReplayStore.h
)
target_include_directories(MultiDetectTargetEventReplayStore PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(MultiDetectTargetEventReplayStore PUBLIC Qt6::Core)
target_compile_features(MultiDetectTargetEventReplayStore PUBLIC cxx_std_20)

add_executable(
    MultiDetectTargetEventReplayStoreSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectTargetEventReplayStoreSelfTest.cc
)
target_link_libraries(
    MultiDetectTargetEventReplayStoreSelfTest PRIVATE MultiDetectTargetEventReplayStore Qt6::Core
)

add_library(
    MultiDetectProducerEpochDiagnostics STATIC
    src/MultiDetectProducerEpochDiagnostics.cc
    src/MultiDetectProducerEpochDiagnostics.h
)
target_include_directories(MultiDetectProducerEpochDiagnostics PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(MultiDetectProducerEpochDiagnostics PUBLIC Qt6::Core)

add_executable(
    MultiDetectProducerEpochDiagnosticsSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectProducerEpochDiagnosticsSelfTest.cc
)
target_link_libraries(
    MultiDetectProducerEpochDiagnosticsSelfTest PRIVATE MultiDetectProducerEpochDiagnostics Qt6::Core
)

add_library(
    MultiDetectTargetObservationDiagnostics STATIC
    src/MultiDetectTargetObservationDiagnostics.cc
    src/MultiDetectTargetObservationDiagnostics.h
)
target_include_directories(MultiDetectTargetObservationDiagnostics PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(MultiDetectTargetObservationDiagnostics PUBLIC Qt6::Core)

add_executable(
    MultiDetectTargetObservationDiagnosticsSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectTargetObservationDiagnosticsSelfTest.cc
)
target_link_libraries(
    MultiDetectTargetObservationDiagnosticsSelfTest PRIVATE MultiDetectTargetObservationDiagnostics Qt6::Core
)

add_library(
    MultiDetectTargetObservationRecorder STATIC
    src/MultiDetectTargetObservationRecorder.cc
    src/MultiDetectTargetObservationRecorder.h
)
target_include_directories(MultiDetectTargetObservationRecorder PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(
    MultiDetectTargetObservationRecorder PUBLIC MultiDetectTargetObservationDiagnostics Qt6::Core
)

add_executable(
    MultiDetectTargetObservationRecorderSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectTargetObservationRecorderSelfTest.cc
)
target_link_libraries(
    MultiDetectTargetObservationRecorderSelfTest PRIVATE MultiDetectTargetObservationRecorder Qt6::Core
)

add_executable(
    MultiDetectDepthGridProtocolSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectDepthGridProtocolSelfTest.cc
)
target_link_libraries(MultiDetectDepthGridProtocolSelfTest PRIVATE MultiDetectOperatorProtocol Qt6::Core)

# E4 synchronized replay uses a local-only RTSP publisher. The stdin wire codec
# remains a small independent target so strict parser/state-machine tests do not
# require a running GStreamer registry or an RTSP client.
add_library(
    MultiDetectReplayRtspWire STATIC
    tools/MultiDetectReplayRtspWire.cc
    tools/MultiDetectReplayRtspWire.h
)
target_include_directories(MultiDetectReplayRtspWire PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/tools)
target_link_libraries(MultiDetectReplayRtspWire PUBLIC Qt6::Core)
target_compile_features(MultiDetectReplayRtspWire PUBLIC cxx_std_20)

add_executable(
    MultiDetectReplayRtspWireSelfTest EXCLUDE_FROM_ALL
    tests/MultiDetectReplayRtspWireSelfTest.cc
)
target_link_libraries(MultiDetectReplayRtspWireSelfTest PRIVATE MultiDetectReplayRtspWire Qt6::Core)

if(QGC_ENABLE_GST_VIDEOSTREAMING AND NOT ANDROID AND NOT IOS)
    add_executable(
        MultiDetectReplayRtspPublisher
        tools/MultiDetectReplayRtspPublisher.cc
    )
    target_link_libraries(MultiDetectReplayRtspPublisher PRIVATE MultiDetectReplayRtspWire)
    target_compile_features(MultiDetectReplayRtspPublisher PRIVATE cxx_std_20)

    # custom/ is configured before src/VideoManager discovers the bundled
    # GStreamer SDK. Defer only the SDK-dependent wiring until the root
    # directory has finished configuring; this avoids a second GStreamer
    # discovery path and guarantees that the publisher uses QGC's exact SDK.
    function(_multidetect_configure_replay_rtsp_publisher)
        if(NOT TARGET MultiDetectReplayRtspPublisher)
            return()
        endif()
        if(NOT TARGET GStreamer::GStreamer OR NOT GStreamer_ROOT_DIR)
            message(FATAL_ERROR
                "MultiDetectReplayRtspPublisher requires QGC's bundled GStreamer target and SDK root"
            )
        endif()

        find_path(
            MULTIDETECT_GST_RTSP_SERVER_INCLUDE_DIR
            NAMES gst/rtsp-server/rtsp-server.h
            HINTS "${GStreamer_ROOT_DIR}/include/gstreamer-1.0"
            NO_DEFAULT_PATH
            REQUIRED
        )
        find_library(
            MULTIDETECT_GST_RTSP_SERVER_LIBRARY
            NAMES gstrtspserver-1.0
            HINTS "${GStreamer_ROOT_DIR}/lib" "${GSTREAMER_LIB_PATH}"
            NO_DEFAULT_PATH
            REQUIRED
        )

        add_library(MultiDetectGstRtspServer UNKNOWN IMPORTED GLOBAL)
        set_target_properties(
            MultiDetectGstRtspServer
            PROPERTIES
                IMPORTED_LOCATION "${MULTIDETECT_GST_RTSP_SERVER_LIBRARY}"
                INTERFACE_INCLUDE_DIRECTORIES "${MULTIDETECT_GST_RTSP_SERVER_INCLUDE_DIR}"
        )
        target_link_libraries(
            MultiDetectReplayRtspPublisher
            PRIVATE
                GStreamer::GStreamer
                MultiDetectGstRtspServer
        )

        if(WIN32)
            find_file(
                MULTIDETECT_GST_RTSP_SERVER_RUNTIME
                NAMES gstrtspserver-1.0-0.dll
                HINTS "${GStreamer_ROOT_DIR}/bin"
                NO_DEFAULT_PATH
                REQUIRED
            )
            install(
                FILES "${MULTIDETECT_GST_RTSP_SERVER_RUNTIME}"
                DESTINATION bin
                COMPONENT Runtime
            )
        endif()
    endfunction()

    cmake_language(
        DEFER
        DIRECTORY "${CMAKE_SOURCE_DIR}"
        CALL _multidetect_configure_replay_rtsp_publisher
    )

    install(TARGETS MultiDetectReplayRtspPublisher RUNTIME DESTINATION bin COMPONENT Runtime)
endif()

# These uppercase cache names are the public QGC custom-build integration API.
# cmake-lint: disable=C0103
set(CUSTOM_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/src/CustomPlugin.cc ${CMAKE_CURRENT_SOURCE_DIR}/src/CustomPlugin.h
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectOperatorController.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectOperatorController.h
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectTargetEvidenceStore.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectTargetEvidenceStore.h
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectTargetEventReplayStore.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/src/MultiDetectTargetEventReplayStore.h
    CACHE INTERNAL "" FORCE
)

set(CUSTOM_LIBRARIES
    MultiDetectUi MultiDetectOperatorProtocol MultiDetectProducerEpochDiagnostics
    MultiDetectTargetObservationDiagnostics MultiDetectTargetObservationRecorder
    MultiDetectTargetEventReplayStore
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
