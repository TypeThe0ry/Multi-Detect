# ============================================================================
# Communications Module
# Handles all communication links (TCP, UDP, Serial, Bluetooth, etc.)
# ============================================================================

# ----------------------------------------------------------------------------
# Core Communication Sources
# ----------------------------------------------------------------------------
target_sources(${CMAKE_PROJECT_NAME}
    PRIVATE
        LinkConfiguration.cc
        LinkConfiguration.h
        LinkInterface.cc
        LinkInterface.h
        LinkManager.cc
        LinkManager.h
        LogReplayLink.cc
        LogReplayLink.h
        LogReplayLinkController.cc
        LogReplayLinkController.h
        MAVLinkProtocol.cc
        MAVLinkProtocol.h
        TCPLink.cc
        TCPLink.h
        UdpIODevice.cc
        UdpIODevice.h
        UDPLink.cc
        UDPLink.h
)

# USB board information JSON data
qt_add_resources(${CMAKE_PROJECT_NAME} json_comm
    PREFIX "/json"
    FILES USBBoardInfo.json
)

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# Uncomment to enable Qt socket debugging
# target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE QABSTRACTSOCKET_DEBUG)

# ============================================================================
# Optional Communication Features
# ============================================================================

# ----------------------------------------------------------------------------
# Serial Port Communication
# ----------------------------------------------------------------------------

if(QGC_NO_SERIAL_LINK)
    target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE QGC_NO_SERIAL_LINK)
else()
    target_sources(${CMAKE_PROJECT_NAME}
        PRIVATE
            QGCSerialPortInfo.cc
            QGCSerialPortInfo.h
            SerialLink.cc
            SerialLink.h
    )

    if(NOT ANDROID)
        target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE Qt6::SerialPort)
    endif()
endif()

# TCPLink and SerialLink intentionally use the same anonymous-namespace timeout
# names. Keep them in separate translation units when the main target uses
# unity compilation.
set(_qgc_comms_unity_unsafe_sources TCPLink.cc)
if(NOT QGC_NO_SERIAL_LINK)
    list(APPEND _qgc_comms_unity_unsafe_sources SerialLink.cc)
endif()
set_source_files_properties(
    ${_qgc_comms_unity_unsafe_sources}
    TARGET_DIRECTORY ${CMAKE_PROJECT_NAME}
    PROPERTIES SKIP_UNITY_BUILD_INCLUSION ON
)

# ----------------------------------------------------------------------------
# Bluetooth Communication
# ----------------------------------------------------------------------------
add_subdirectory(Bluetooth)

# ============================================================================
# Communication Link Subdirectories
# ============================================================================

add_subdirectory(MockLink)
