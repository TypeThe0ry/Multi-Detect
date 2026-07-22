# ============================================================================
# Firmware Plugin Module
# Provides autopilot-specific firmware handling and communication
# ============================================================================

target_sources(${CMAKE_PROJECT_NAME}
    PRIVATE
        FirmwarePlugin.cc
        FirmwarePlugin.h
        FirmwarePluginFactory.cc
        FirmwarePluginFactory.h
        FirmwarePluginManager.cc
        FirmwarePluginManager.h
        ParameterMetaData.cc
        ParameterMetaData.h
)

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# ----------------------------------------------------------------------------
# Firmware-Specific Plugins
# ----------------------------------------------------------------------------
if(NOT QGC_DISABLE_APM_PLUGIN)
    add_subdirectory(APM)
endif()

if(NOT QGC_DISABLE_PX4_PLUGIN)
    add_subdirectory(PX4)
endif()
