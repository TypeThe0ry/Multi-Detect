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
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionSecurity.cc"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectMissionSecurity.h"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectOperatorProtocol.cc"
    "${MULTIDETECT_CUSTOM_DIR}/src/MultiDetectOperatorProtocol.h"
)
target_compile_features(MultiDetectMissionPackageCheck PUBLIC cxx_std_20)
target_include_directories(MultiDetectMissionPackageCheck PUBLIC "${MULTIDETECT_CUSTOM_DIR}/src")
target_link_libraries(MultiDetectMissionPackageCheck PUBLIC Qt6::Core)
set(MULTIDETECT_OPENSSL_ROOT "" CACHE PATH "OpenSSL root containing lib/cmake/OpenSSL or include + lib")
find_package(OpenSSL QUIET COMPONENTS Crypto
    HINTS "${MULTIDETECT_OPENSSL_ROOT}" "$ENV{OPENSSL_ROOT_DIR}")
if(TARGET OpenSSL::Crypto)
    message(STATUS "Mission package check: using OpenSSL Crypto Ed25519 backend")
    target_compile_definitions(MultiDetectMissionPackageCheck PRIVATE MULTIDETECT_HAVE_OPENSSL=1)
    target_link_libraries(MultiDetectMissionPackageCheck PUBLIC OpenSSL::Crypto)
endif()

add_executable(MultiDetectMissionSelfTest "${MULTIDETECT_CUSTOM_DIR}/tests/MultiDetectMissionSelfTest.cc")
target_link_libraries(MultiDetectMissionSelfTest PRIVATE MultiDetectMissionPackageCheck)

add_executable(MultiDetectMissionSecuritySelfTest
    "${MULTIDETECT_CUSTOM_DIR}/tests/MultiDetectMissionSecuritySelfTest.cc"
)
target_link_libraries(MultiDetectMissionSecuritySelfTest PRIVATE MultiDetectMissionPackageCheck)

add_executable(
    MultiDetectOperatorProtocolSelfTest
    "${MULTIDETECT_CUSTOM_DIR}/tests/MultiDetectOperatorProtocolSelfTest.cc"
)
target_link_libraries(MultiDetectOperatorProtocolSelfTest PRIVATE MultiDetectMissionPackageCheck)

enable_testing()
add_test(NAME MultiDetectMissionSelfTest COMMAND MultiDetectMissionSelfTest)
add_test(NAME MultiDetectMissionSecuritySelfTest COMMAND MultiDetectMissionSecuritySelfTest)
add_test(NAME MultiDetectOperatorProtocolSelfTest COMMAND MultiDetectOperatorProtocolSelfTest)
