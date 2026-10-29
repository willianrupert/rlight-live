// OLED 1,3" + INA226 no I2C (GPIO8 SDA / GPIO9 SCL). Cada peça é opcional: sem ela
// no barramento, o resto do terminal funciona igual.
#pragma once
#include <stdbool.h>

typedef enum { TELA_SEM_SERVIDOR, TELA_ESPERANDO, TELA_OUVINDO } tela_estado_t;

void tela_iniciar(const char *palavra);
void tela_estado(tela_estado_t e);
void tela_falando(bool falando);
void tela_texto(char quem, const char *t);   // 'v' = você, 'r' = RLight (texto acumulado)

// no main.c: manda a leitura da bateria para o servidor (a cada 10 s)
void rlight_enviar_bateria(float v_bat, float ma_saida, int pct);
