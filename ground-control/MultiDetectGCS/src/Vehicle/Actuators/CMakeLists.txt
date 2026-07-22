# ============================================================================
# Actuators Subsystem
# Motor, servo, and actuator configuration and testing
# ============================================================================

set(_qgc_actuator_unity_unsafe_sources
    ActuatorActions.cc
    ActuatorOutputs.cc
    Actuators.cc
    ActuatorTesting.cc
    Common.cc
    GeometryImage.cc
    Mixer.cc
    MotorAssignment.cc
)

target_sources(${CMAKE_PROJECT_NAME}
    PRIVATE
        ${_qgc_actuator_unity_unsafe_sources}
        ActuatorActions.h
        ActuatorOutputs.h
        Actuators.h
        ActuatorTesting.h
        Common.h
        GeometryImage.h
        Mixer.h
        MotorAssignment.h
)

# These implementation files use file-scope `using namespace` directives for
# namespaces that intentionally contain identically named types. Combining the
# files in one unity translation unit makes otherwise valid qualified member
# definitions ambiguous on MSVC. Keep this small subsystem as standalone
# translation units while retaining unity compilation for the rest of QGC.
set_source_files_properties(
    ${_qgc_actuator_unity_unsafe_sources}
    TARGET_DIRECTORY ${CMAKE_PROJECT_NAME}
    PROPERTIES SKIP_UNITY_BUILD_INCLUSION ON
)

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
