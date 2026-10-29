// Palavra de ativação "Ei, Luma" (microWakeWord, ver ../palavra_luma/). Só compila com
// CONFIG_RLIGHT_PALAVRA_LUMA e o modelo em main/palavra/ei_luma.tflite.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
bool luma_iniciar(void);
bool luma_processar(const int16_t *pcm, int amostras);   // true = palavra detectada
void luma_zerar(void);
#ifdef __cplusplus
}
#endif
