cmake_minimum_required(VERSION 3.16)
project(x1_wind_airspeed LANGUAGES CXX)

include(CTest)
option(X1_BUILD_GZ_PLUGIN "Build against the Gazebo Harmonic SDK" ON)
set(X1_GZ_SNAPSHOT "" CACHE PATH "Optional x86_64 simulator image snapshot root")

if(X1_BUILD_GZ_PLUGIN)
  add_library(x1_wind_airspeed MODULE WindAirspeed.cc)
  target_compile_features(x1_wind_airspeed PRIVATE cxx_std_17)
  if(X1_GZ_SNAPSHOT)
    # This image layer has the SDK, but not all the public CMake targets'
    # transitive development headers (notably cppzmq). Use the required ELF libs.
    set(sdk_include "${X1_GZ_SNAPSHOT}/usr/include")
    set(sdk_library "${X1_GZ_SNAPSHOT}/usr/lib/x86_64-linux-gnu")
    foreach(api sim8 plugin2 common5 sensors8 math7 utils2 msgs10 transport13 sdformat14)
      target_include_directories(x1_wind_airspeed SYSTEM PRIVATE "${sdk_include}/gz/${api}")
    endforeach()
    target_include_directories(x1_wind_airspeed SYSTEM PRIVATE "${sdk_include}/eigen3")
    target_compile_options(x1_wind_airspeed PRIVATE
      "SHELL:-idirafter '${sdk_include}'" "SHELL:-idirafter '${sdk_include}/x86_64-linux-gnu'")
    foreach(lib gz-sim8 gz-sensors8-air_speed gz-sensors8 gz-common5 gz-math7
                sdformat14 gz-msgs10 protobuf)
      target_link_libraries(x1_wind_airspeed PRIVATE "${sdk_library}/lib${lib}.so")
    endforeach()
    target_link_options(x1_wind_airspeed PRIVATE "LINKER:-rpath-link,${sdk_library}")
  else()
    find_package(gz-sim8 REQUIRED)
    find_package(gz-plugin2 REQUIRED COMPONENTS register)
    find_package(gz-sensors8 REQUIRED COMPONENTS air_speed)
    target_link_libraries(x1_wind_airspeed PRIVATE
      gz-sim8::core gz-plugin2::register gz-sensors8::air_speed)
  endif()
  target_link_options(x1_wind_airspeed PRIVATE "LINKER:--no-undefined")
  # The mounted module must resolve the running image's libraries, not the SDK.
  set_target_properties(x1_wind_airspeed PROPERTIES SKIP_BUILD_RPATH TRUE)
endif()

if(BUILD_TESTING)
  add_executable(test_wind_projection test_wind_projection.cc)
  target_compile_features(test_wind_projection PRIVATE cxx_std_17)
  add_test(NAME wind_projection COMMAND test_wind_projection)
endif()
