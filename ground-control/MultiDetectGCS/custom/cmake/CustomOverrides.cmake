# Keep this custom build focused on the Pixhawk V6X/PX4 aircraft used by Multi-Detect.

# The root manifest is the single version source for Python, QGC and release
# artifacts. QGC's project() call still requires a numeric version, while the
# About/build surfaces and artifact paths retain the prerelease label.
if(NOT MULTIDETECT_PRODUCT_VERSION_MANIFEST)
    get_filename_component(
        MULTIDETECT_PRODUCT_VERSION_MANIFEST
        "${CMAKE_SOURCE_DIR}/../../configs/product-version.json"
        ABSOLUTE
    )
endif()
set(
    MULTIDETECT_PRODUCT_VERSION_MANIFEST
    "${MULTIDETECT_PRODUCT_VERSION_MANIFEST}"
    CACHE FILEPATH "Canonical Multi-Detect product version manifest" FORCE
)
if(NOT EXISTS "${MULTIDETECT_PRODUCT_VERSION_MANIFEST}")
    message(FATAL_ERROR
        "Multi-Detect product version manifest is missing: "
        "${MULTIDETECT_PRODUCT_VERSION_MANIFEST}"
    )
endif()

file(READ "${MULTIDETECT_PRODUCT_VERSION_MANIFEST}" _multidetect_version_json)
string(JSON _multidetect_version_member_count LENGTH "${_multidetect_version_json}")
if(NOT _multidetect_version_member_count EQUAL 6)
    message(FATAL_ERROR
        "Multi-Detect product version manifest must contain exactly six fields"
    )
endif()
set(_multidetect_version_expected_members
    channel numeric product python release_label schema_version
)
math(EXPR _multidetect_version_last_member "${_multidetect_version_member_count} - 1")
foreach(_multidetect_version_member_index RANGE 0 ${_multidetect_version_last_member})
    string(JSON _multidetect_version_member MEMBER
        "${_multidetect_version_json}" ${_multidetect_version_member_index}
    )
    if(NOT _multidetect_version_member IN_LIST _multidetect_version_expected_members)
        message(FATAL_ERROR
            "Unsupported field in Multi-Detect product version manifest: "
            "${_multidetect_version_member}"
        )
    endif()
endforeach()
string(JSON _multidetect_version_schema_type TYPE "${_multidetect_version_json}" schema_version)
if(NOT _multidetect_version_schema_type STREQUAL "NUMBER")
    message(FATAL_ERROR "Multi-Detect product version schema_version must be an integer")
endif()
foreach(_multidetect_string_field product release_label numeric python channel)
    string(JSON _multidetect_string_type TYPE
        "${_multidetect_version_json}" ${_multidetect_string_field}
    )
    if(NOT _multidetect_string_type STREQUAL "STRING")
        message(FATAL_ERROR
            "Multi-Detect product version ${_multidetect_string_field} must be a string"
        )
    endif()
endforeach()
string(JSON _multidetect_version_schema GET "${_multidetect_version_json}" schema_version)
string(JSON _multidetect_product_name GET "${_multidetect_version_json}" product)
string(JSON _multidetect_release_label GET "${_multidetect_version_json}" release_label)
string(JSON _multidetect_numeric_version GET "${_multidetect_version_json}" numeric)
string(JSON _multidetect_python_version GET "${_multidetect_version_json}" python)
string(JSON _multidetect_release_channel GET "${_multidetect_version_json}" channel)

if(NOT _multidetect_version_schema STREQUAL "1")
    message(FATAL_ERROR
        "Unsupported Multi-Detect product version schema: ${_multidetect_version_schema}"
    )
endif()
if(NOT _multidetect_product_name STREQUAL "Multi-Detect")
    message(FATAL_ERROR
        "Unexpected product in Multi-Detect version manifest: ${_multidetect_product_name}"
    )
endif()
if(NOT _multidetect_numeric_version MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
    message(FATAL_ERROR
        "Invalid numeric version in product manifest: ${_multidetect_numeric_version}"
    )
endif()
string(REPLACE "." "\\." _multidetect_numeric_regex "${_multidetect_numeric_version}")
if(_multidetect_release_channel STREQUAL "prerelease")
    if(NOT _multidetect_release_label MATCHES
        "^${_multidetect_numeric_regex}-pre\\.([1-9][0-9]*)$"
    )
        message(FATAL_ERROR
            "Prerelease label does not match numeric version: ${_multidetect_release_label}"
        )
    endif()
    set(_multidetect_label_sequence "${CMAKE_MATCH_1}")
    if(NOT _multidetect_python_version MATCHES
        "^${_multidetect_numeric_regex}rc([1-9][0-9]*)$"
    )
        message(FATAL_ERROR
            "Python prerelease version does not match numeric version: "
            "${_multidetect_python_version}"
        )
    endif()
    set(_multidetect_python_sequence "${CMAKE_MATCH_1}")
    if(NOT "${_multidetect_label_sequence}" STREQUAL "${_multidetect_python_sequence}")
        message(FATAL_ERROR
            "Release label and Python prerelease sequences differ: "
            "${_multidetect_label_sequence} != ${_multidetect_python_sequence}"
        )
    endif()
elseif(_multidetect_release_channel STREQUAL "release")
    if(NOT "${_multidetect_release_label}" STREQUAL "${_multidetect_numeric_version}"
       OR NOT "${_multidetect_python_version}" STREQUAL "${_multidetect_numeric_version}")
        message(FATAL_ERROR
            "Release channel requires release_label and python to equal numeric"
        )
    endif()
else()
    message(FATAL_ERROR
        "Unsupported Multi-Detect release channel: ${_multidetect_release_channel}"
    )
endif()

set(QGC_APP_NAME
    "MultiDetectGCS"
    CACHE STRING "CMake target and executable name" FORCE
)
set(QGC_APP_VERSION_OVERRIDE
    "${_multidetect_numeric_version}"
    CACHE STRING "Multi-Detect numeric product version" FORCE
)
set(QGC_APP_RELEASE_LABEL
    "${_multidetect_release_label}"
    CACHE STRING "Multi-Detect human-facing release label" FORCE
)
set(QGC_WINDOWS_INSTALLER_FILENAME
    "MultiDetectGCS-v${_multidetect_release_label}-windows-amd64.exe"
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
