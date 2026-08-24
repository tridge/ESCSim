cmake_minimum_required(VERSION 3.18)

project(am32sim VERSION 0.1.0 LANGUAGES C)

include(CTest)
find_package(Threads REQUIRED)

add_library(am32sim SHARED
    src/am32sim_shim.c
    src/jsmn.c
    src/motor.c
    src/sitl_config.c
)

target_include_directories(am32sim
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>
)
target_compile_features(am32sim PRIVATE c_std_11)
target_compile_definitions(am32sim PRIVATE _DEFAULT_SOURCE)
target_compile_options(am32sim PRIVATE
    $<$<C_COMPILER_ID:GNU,Clang>:-Wall;-Wextra;-Wno-unused-parameter>
)
target_link_libraries(am32sim PRIVATE Threads::Threads)
if(NOT WIN32)
    target_link_libraries(am32sim PRIVATE m)
endif()
set_target_properties(am32sim PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    WINDOWS_EXPORT_ALL_SYMBOLS ON
)
if(WIN32)
    set_target_properties(am32sim PROPERTIES PREFIX "")
endif()

install(TARGETS am32sim
    RUNTIME DESTINATION escsim/renode/lib
    LIBRARY DESTINATION escsim/renode/lib
    ARCHIVE DESTINATION escsim/renode/lib
)

if(BUILD_TESTING)
    add_executable(am32sim_smoke tests/smoke.c)
    target_link_libraries(am32sim_smoke PRIVATE am32sim)
    add_test(NAME am32sim_smoke COMMAND am32sim_smoke)
endif()
