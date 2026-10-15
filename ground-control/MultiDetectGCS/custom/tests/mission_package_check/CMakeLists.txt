cmake_minimum_required(VERSION 3.24)

# Standalone, offline check for the mission package and existing operator wire.
# Only an already-installed Qt Core and C++20 compiler are required.
project(MultiDetectMissionPackageCheck LANGUAGES CXX)

set(CMAKE_AUTOMOC ON)
find_package(Qt6 REQUIRED COMPONENTS Core)

get_filename_component(MULTIDETECT_CUSTOM_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

add_library(
    MultiDetectMissionPackageCheck STATIC
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionWire.cc"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionWire.h"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionPackage.cc"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionPackage.h"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionController.cc"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionController.h"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectOperatorProtocol.cc"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectOperatorProtocol.h"
)
target_compile_features(MultiDetectMissionPackageCheck PUBLIC cxx_std_20)
target_include_directories(MultiDetectMissionPackageCheck PUBLIC "${MULTIDETECT_CUSTOM_DIR}/src")
target_link_libraries(MultiDetectMissionPackageCheck PUBLIC Qt6::Core)

add_executable(MultiDetectMissionSelfTest "${MULTIDETECT_CUSTOM_DIR}/tests/MultiDetectMissionSelfTest.cc")
target_link_libraries(MultiDetectMissionSelfTest PRIVATE MultiDetectMissionPackageCheck)

add_executable(
    MultiDetectOperatorProtocolSelfTest
    "${MULTIDETECT_CUSTOM_DIR}/tests/MultiDetectOperatorProtocolSelfTest.cc"
)
target_link_libraries(MultiDetectOperatorProtocolSelfTest PRIVATE MultiDetectMissionPackageCheck)

enable_testing()
add_test(NAME MultiDetectMissionSelfTest COMMAND MultiDetectMissionSelfTest)
add_test(NAME MultiDetectOperatorProtocolSelfTest COMMAND MultiDetectOperatorProtocolSelfTest)
