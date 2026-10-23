cmake_minimum_required(VERSION 3.16)
project(HoloTestbedTelemetry VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 编译优化参数
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-O3 -Wall -Wextra -pthread)
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64")
        add_compile_options(-march=armv8.2-a)
    endif()
endif()

include_directories(include)

# 编译带外 PMU 守护进程
add_executable(holo_pmu_daemon src/telemetry_daemon.cpp)
target_link_libraries(holo_pmu_daemon PRIVATE pthread rt)

install(TARGETS holo_pmu_daemon DESTINATION bin)
