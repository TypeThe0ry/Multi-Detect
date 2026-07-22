cmake_minimum_required(VERSION 3.25)

include("${CMAKE_CURRENT_LIST_DIR}/_assert.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/../FileAliases.cmake")

set(_sandbox "${CMAKE_CURRENT_BINARY_DIR}/file-alias-sandbox")
file(REMOVE_RECURSE "${_sandbox}")
file(MAKE_DIRECTORY "${_sandbox}/lib/pkgconfig")
file(WRITE "${_sandbox}/lib/pkgconfig/libpng16.pc" "libpng16-payload\n")

gstreamer_materialize_file_alias(
    "${_sandbox}"
    "lib/pkgconfig/libpng.pc"
    "lib/pkgconfig/libpng16.pc"
)
file(READ "${_sandbox}/lib/pkgconfig/libpng.pc" _actual)
qgc_test_assert_streq("alias content" "libpng16-payload\n" "${_actual}")

# Existing aliases are preserved so a native symlink or SDK-provided regular
# file is never replaced on a subsequent configure.
file(WRITE "${_sandbox}/lib/pkgconfig/libpng.pc" "existing-alias\n")
gstreamer_materialize_file_alias(
    "${_sandbox}"
    "lib/pkgconfig/libpng.pc"
    "lib/pkgconfig/libpng16.pc"
)
file(READ "${_sandbox}/lib/pkgconfig/libpng.pc" _actual)
qgc_test_assert_streq("existing alias preserved" "existing-alias\n" "${_actual}")

qgc_test_pass("GStreamer file alias materialization")
