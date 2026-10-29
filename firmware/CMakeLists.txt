cmake_minimum_required(VERSION 3.16)
# sdkconfig.segredos (fora do git): Wi-Fi, servidor e token. Ver o README.
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.segredos")
    set(SDKCONFIG_DEFAULTS "sdkconfig.defaults;sdkconfig.segredos")
endif()
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rlight_terminal)
