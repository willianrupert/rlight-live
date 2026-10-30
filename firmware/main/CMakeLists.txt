set(srcs "main.c" "tela.c")
set(embed "")
set(reqs esp_wifi esp_event esp_netif nvs_flash esp_driver_i2s esp_driver_i2c esp_driver_gpio esp_timer json app_update esp_http_client esp_app_format)
# "Ei, Luma" (microWakeWord): precisa do modelo treinado no Colab (../palavra_luma/)
if(CONFIG_RLIGHT_PALAVRA_LUMA)
    list(APPEND srcs "luma.cc")
    list(APPEND embed "palavra/ei_luma.tflite")
    list(APPEND reqs esp-tflite-micro esp-micro-speech-features)
endif()
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "."
                       EMBED_FILES ${embed}
                       REQUIRES ${reqs})
