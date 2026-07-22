include_guard(GLOBAL)

# Materialize an archive symlink as a regular file. Some Windows extractors
# cannot create symlinks without Developer Mode/elevation and silently omit
# them, which leaves otherwise complete SDKs unusable by pkg-config.
function(gstreamer_materialize_file_alias ROOT ALIAS_REL TARGET_REL)
    foreach(_rel IN ITEMS "${ALIAS_REL}" "${TARGET_REL}")
        if(IS_ABSOLUTE "${_rel}" OR _rel MATCHES "(^|[/\\\\])\\.\\.([/\\\\]|$)")
            message(FATAL_ERROR "GStreamer: file alias path must stay relative to the SDK root: ${_rel}")
        endif()
    endforeach()

    set(_alias "${ROOT}/${ALIAS_REL}")
    set(_target "${ROOT}/${TARGET_REL}")
    if(EXISTS "${_alias}")
        return()
    endif()
    if(NOT EXISTS "${_target}")
        message(FATAL_ERROR
            "GStreamer: cannot materialize missing SDK alias '${ALIAS_REL}'; "
            "target '${TARGET_REL}' does not exist under ${ROOT}")
    endif()

    get_filename_component(_alias_dir "${_alias}" DIRECTORY)
    file(MAKE_DIRECTORY "${_alias_dir}")
    configure_file("${_target}" "${_alias}" COPYONLY)
    message(STATUS "GStreamer: Materialized SDK file alias ${ALIAS_REL} -> ${TARGET_REL}")
endfunction()
