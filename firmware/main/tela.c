// Tela e bateria do terminal. Uma tarefa só é dona do I2C: lê o INA226 a cada 10 s e
// redesenha a OLED quando algo muda. A OLED apaga depois de 30 s esperando a palavra.
#include "tela.h"

#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "driver/gpio.h"
#include "sdkconfig.h"
#include "fonte5x7.h"

#define I2C_SDA 8
#define I2C_SCL 9
#define INA_END 0x40
#define OLED_END 0x3C
#define TELA_APAGA_US (30 * 1000000LL)

static const char *TAG = "tela";
static i2c_master_dev_handle_t ina, oled;
static SemaphoreHandle_t mtx;
static uint8_t fb[8][128];
static char txt_voce[160], txt_rlight[480];
static const char *palavra_txt;
static volatile tela_estado_t estado = TELA_SEM_SERVIDOR;
static volatile bool falando, sujo = true;
static int64_t ultimo_evento;
static float v_bat, ma;
static int pct = -1;
static int leituras_baixas;

// ---------- INA226: VBS = tensão das células, shunt = consumo na saída 5 V ----------
static bool ina_reg(uint8_t reg, uint16_t *v)
{
    uint8_t rx[2];
    if (i2c_master_transmit_receive(ina, &reg, 1, rx, 2, 50) != ESP_OK) return false;
    *v = (rx[0] << 8) | rx[1];
    return true;
}

static int soc(float v)   // tensão de circuito aberto → % (NMC, INR18650-M29)
{
    static const float t[][2] = { {4.20, 100}, {4.10, 90}, {4.00, 80}, {3.92, 70}, {3.85, 60},
        {3.79, 50}, {3.75, 40}, {3.71, 30}, {3.67, 20}, {3.61, 10}, {3.50, 5}, {3.30, 0} };
    if (v >= t[0][0]) return 100;
    for (int i = 1; i < sizeof(t) / sizeof(t[0]); i++)
        if (v >= t[i][0])
            return (int)(t[i][1] + (v - t[i][0]) / (t[i - 1][0] - t[i][0]) * (t[i - 1][1] - t[i][1]) + 0.5f);
    return 0;
}

static void oled_ligar(bool on);
static void desenhar(void);
static void escrever(int linha, int col, const char *s);
static void oled_enviar(void);

// Bateria no fim: mostra "carregue", desliga o que dá e dorme 10 min. Ao acordar o boot
// mede de novo: carregando (tensão subiu) volta ao normal; senão, dorme outra vez.
static void dormir_bateria_fraca(void)
{
    char l[32];
    ESP_LOGW(TAG, "bateria %.2f V < %.2f V: dormindo para proteger as células",
             v_bat, CONFIG_RLIGHT_BATERIA_CORTE_MV / 1000.0f);
    if (oled) {
        memset(fb, 0, sizeof(fb));
        escrever(1, 0, "BATERIA FRACA");
        snprintf(l, sizeof(l), "%.2f V  %d%%", v_bat, pct);
        escrever(3, 0, l);
        escrever(5, 0, "carregue o terminal");
        oled_enviar();
        vTaskDelay(pdMS_TO_TICKS(5000));
        oled_ligar(false);
    }
    if (ina) {                                       // INA226 em power-down (~2 µA)
        uint8_t cfg[3] = {0x00, 0x47, 0x20};
        i2c_master_transmit(ina, cfg, 3, 50);
    }
    if (CONFIG_RLIGHT_AMP_SD_GPIO >= 0) {           // amplificador desligado durante o sono
        gpio_set_level(CONFIG_RLIGHT_AMP_SD_GPIO, 0);
        gpio_hold_en(CONFIG_RLIGHT_AMP_SD_GPIO);
        gpio_deep_sleep_hold_en();
    }
    esp_sleep_enable_timer_wakeup(10ULL * 60 * 1000000);
    esp_deep_sleep_start();
}

static void ler_bateria(void)
{
    uint16_t sh, bus;
    if (!ina || !ina_reg(0x01, &sh) || !ina_reg(0x02, &bus)) return;
    v_bat = bus * 1.25e-3f;                                            // LSB 1,25 mV
    ma = (int16_t)sh * 2.5e-3f / (CONFIG_RLIGHT_INA_SHUNT_MOHM / 1000.0f);   // 2,5 µV / R
    pct = soc(v_bat);
    sujo = true;
    rlight_enviar_bateria(v_bat, ma, pct);
    // 3 leituras seguidas (30 s) abaixo do corte: um pico de som não derruba o terminal
    leituras_baixas = v_bat > 2.5f && v_bat * 1000 < CONFIG_RLIGHT_BATERIA_CORTE_MV ? leituras_baixas + 1 : 0;
    if (leituras_baixas >= 3) dormir_bateria_fraca();
}

// ---------- OLED (SH1106 132 colunas ou SSD1306 128, modo página) ----------
static void oled_cmd(const uint8_t *c, size_t n)
{
    uint8_t b[16] = {0x00};
    memcpy(b + 1, c, n);
    i2c_master_transmit(oled, b, n + 1, 50);
}

static void oled_ligar(bool on)
{
    uint8_t c = on ? 0xAF : 0xAE;
    oled_cmd(&c, 1);
}

static void oled_enviar(void)
{
    const uint8_t col = CONFIG_RLIGHT_OLED_SH1106 ? 2 : 0;
    uint8_t b[129] = {0x40};
    for (int p = 0; p < 8; p++) {
        uint8_t c[3] = {0xB0 | p, col & 0x0F, 0x10 | (col >> 4)};
        oled_cmd(c, 3);
        memcpy(b + 1, fb[p], 128);
        i2c_master_transmit(oled, b, sizeof(b), 50);
    }
}

static void escrever(int linha, int col, const char *s)
{
    for (; *s && col <= 21 - 1; s++, col++) {
        unsigned char ch = *s;
        if (ch < 0x20 || ch > 0x7E) ch = '?';
        memcpy(&fb[linha][col * 6], FONTE5X7[ch - 0x20], 5);
    }
}

// quebra em linhas de 21 caracteres e devolve as últimas `max`
static int quebrar(const char *s, char out[][22], int max)
{
    int n = 0, len = strlen(s);
    int total = (len + 20) / 21;
    for (int i = total > max ? total - max : 0; i < total; i++, n++) {
        strncpy(out[n], s + i * 21, 21);
        out[n][21] = 0;
    }
    return n;
}

static void desenhar(void)
{
    char l[32];
    memset(fb, 0, sizeof(fb));
    const char *st = estado == TELA_SEM_SERVIDOR ? "sem servidor"
                   : estado == TELA_ESPERANDO ? "esperando"
                   : falando ? "falando" : "ouvindo";
    escrever(0, 0, st);
    if (pct >= 0) {
        snprintf(l, sizeof(l), "%3d%%", pct);
        escrever(0, 17, l);
    }
    memset(fb[1], 0x01, 128);                                // linha separadora

    if (estado == TELA_OUVINDO) {                            // conversa: você + RLight
        char v[2][22], r[6][22], tv[164];
        xSemaphoreTake(mtx, portMAX_DELAY);
        snprintf(tv, sizeof(tv), "> %s", txt_voce);
        int nv = txt_voce[0] ? quebrar(tv, v, 2) : 0;        // pergunta: últimas 2 linhas
        int nr = quebrar(txt_rlight, r, 6 - nv);             // resposta: o que couber
        xSemaphoreGive(mtx);
        int y = 2;
        for (int i = 0; i < nv; i++) escrever(y++, 0, v[i]);
        for (int i = 0; i < nr; i++) escrever(y++, 0, r[i]);
    } else {
        snprintf(l, sizeof(l), "diga \"%s\"", palavra_txt);
        escrever(3, 0, l);
        if (pct >= 0) {
            snprintf(l, sizeof(l), "bateria %.2f V", v_bat);
            escrever(5, 0, l);
            snprintf(l, sizeof(l), "consumo %.2f W", ma * 5.0f / 1000.0f);
            escrever(6, 0, l);
        }
    }
    oled_enviar();
}

static bool oled_iniciar(void)
{
    static const uint8_t ini_comum[] = {0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40,
                                        0xA1, 0xC8, 0xDA, 0x12, 0x81, 0x80, 0xDB, 0x40, 0xA4, 0xA6};
    oled_cmd(ini_comum, sizeof(ini_comum));
#if CONFIG_RLIGHT_OLED_SH1106
    static const uint8_t ini[] = {0xAD, 0x8B, 0xD9, 0x22};    // DC-DC liga, pré-carga
#else
    static const uint8_t ini[] = {0x8D, 0x14, 0xD9, 0xF1, 0x20, 0x02};   // charge pump, modo página
#endif
    oled_cmd(ini, sizeof(ini));
    desenhar();
    oled_ligar(true);
    return true;
}

static void tarefa_tela(void *arg)
{
    int64_t ultima_bat = -10 * 1000000LL;
    bool acesa = oled != NULL;
    for (;;) {
        int64_t agora = esp_timer_get_time();
        if (agora - ultima_bat >= 10 * 1000000LL) {
            ultima_bat = agora;
            ler_bateria();
        }
        if (oled) {
            bool deve = estado != TELA_ESPERANDO || agora - ultimo_evento < TELA_APAGA_US;
            if (deve && sujo) {
                sujo = false;
                desenhar();
            }
            if (deve != acesa) {                             // economia: apaga esperando
                oled_ligar(deve);
                acesa = deve;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(150));
    }
}

static void evento(void)
{
    ultimo_evento = esp_timer_get_time();
    sujo = true;
}

void tela_estado(tela_estado_t e)
{
    if (e == TELA_OUVINDO && estado != TELA_OUVINDO) {      // conversa nova: limpa
        xSemaphoreTake(mtx, portMAX_DELAY);
        txt_voce[0] = txt_rlight[0] = 0;
        xSemaphoreGive(mtx);
    }
    estado = e;
    evento();
}

void tela_falando(bool f)
{
    if (f != falando) {
        falando = f;
        evento();
    }
}

void tela_texto(char quem, const char *t)
{
    xSemaphoreTake(mtx, portMAX_DELAY);
    if (quem == 'v') {
        strlcpy(txt_voce, t, sizeof(txt_voce));
        txt_rlight[0] = 0;                                   // nova pergunta: resposta nova
    } else {
        strlcpy(txt_rlight, t, sizeof(txt_rlight));
    }
    xSemaphoreGive(mtx);
    evento();
}

void tela_iniciar(const char *palavra)
{
    palavra_txt = palavra;
    mtx = xSemaphoreCreateMutex();
    i2c_master_bus_handle_t bus;
    i2c_master_bus_config_t bc = {
        .i2c_port = I2C_NUM_0, .sda_io_num = I2C_SDA, .scl_io_num = I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    if (i2c_new_master_bus(&bc, &bus) != ESP_OK) return;
    i2c_device_config_t dc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .scl_speed_hz = 400000 };
    if (i2c_master_probe(bus, INA_END, 50) == ESP_OK) {
        dc.device_address = INA_END;
        i2c_master_bus_add_device(bus, &dc, &ina);
        // média de 64 amostras, 1,1 ms por conversão, contínuo (shunt + barramento)
        uint8_t cfg[3] = {0x00, 0x47, 0x27};
        i2c_master_transmit(ina, cfg, 3, 50);
        ESP_LOGI(TAG, "INA226 encontrado (shunt %d mΩ)", CONFIG_RLIGHT_INA_SHUNT_MOHM);
    } else {
        ESP_LOGW(TAG, "INA226 não respondeu em 0x40: sem leitura de bateria");
    }
    if (i2c_master_probe(bus, OLED_END, 50) == ESP_OK) {
        dc.device_address = OLED_END;
        i2c_master_bus_add_device(bus, &dc, &oled);
        oled_iniciar();
        ESP_LOGI(TAG, "OLED encontrada (%s)", CONFIG_RLIGHT_OLED_SH1106 ? "SH1106" : "SSD1306");
    } else {
        ESP_LOGW(TAG, "OLED não respondeu em 0x3C: sem tela");
    }
    if (ina && esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER) {
        vTaskDelay(pdMS_TO_TICKS(100));             // primeira conversão do INA226
        leituras_baixas = 2;                        // uma leitura baixa basta
        ler_bateria();
    }
    evento();
    xTaskCreatePinnedToCore(tarefa_tela, "tela", 4096, NULL, 2, NULL, 0);
}
