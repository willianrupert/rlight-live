/*
 * RLight Live — terminal físico (ESP32-S3 N16R8 + INMP441 + MAX98357A).
 *
 * mic (I2S1) ─┐
 *             ├─ AFE do ESP-SR: AEC (referência = o que vai pro alto-falante), VAD,
 * ref ────────┘  WakeNet → WebSocket /dispositivo do voz_server.py (PCM 16 kHz)
 * WebSocket (voz do RLight, PCM 16 kHz) → fila na PSRAM → volume → I2S0 + referência
 *
 * O servidor faz a ponte com o Gemini Live; a chave nunca vem pra cá. Protocolo em
 * servidor/dispositivo.py. Bipe subindo = começou a ouvir; descendo = parou de ouvir.
 */
#include <math.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "cJSON.h"
#include "driver/i2s_std.h"
#include "esp_websocket_client.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_afe_config.h"
#include "model_path.h"
#include "led_strip.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "tela.h"
#if CONFIG_RLIGHT_PALAVRA_LUMA
#include "luma.h"
#endif

static const char *TAG = "rlight";

// Pinos (evitando 35–37 da PSRAM octal, 0/3/45/46 de strapping e 19/20 do USB)
#define MIC_SCK  5    // INMP441 em I2S1, L/R no GND (canal esquerdo)
#define MIC_WS   6
#define MIC_SD   7
#define SPK_BCLK 15   // MAX98357A em I2S0, VIN 5 V, SD e GAIN soltos (9 dB)
#define SPK_LRC  16
#define SPK_DIN  17

#define TAXA          16000
#define PLAY_AMOSTRAS 256                 // 16 ms por escrita no I2S0
#define FILA_PLAY     (2 * 1024 * 1024)   // ~65 s de voz na PSRAM
#define FILA_REF      (16 * 1024)

typedef enum { ESPERANDO, SESSAO } estado_t;
typedef enum { NADA, ABRIR_AGENTE, FECHAR } pedido_t;

static i2s_chan_handle_t rx, tx;
static StreamBufferHandle_t fila_play, fila_ref;
static SemaphoreHandle_t mtx_play;
static esp_websocket_client_handle_t ws;
static const esp_afe_sr_iface_t *afe;
static esp_afe_sr_data_t *afe_dados;
static const char *palavra = "?";
static volatile estado_t estado = ESPERANDO;
static volatile pedido_t pedido = NADA;    // vem do WebSocket, aplicado na tarefa do AFE
static volatile bool ws_ok;
static led_strip_handle_t led;
static EventGroupHandle_t eventos_wifi;

// ---------- LED ----------
static void cor(uint8_t r, uint8_t g, uint8_t b)
{
    if (!led) return;
    led_strip_set_pixel(led, 0, r, g, b);
    led_strip_refresh(led);
}

static void cor_estado(void)
{
    tela_estado(!ws_ok ? TELA_SEM_SERVIDOR : estado == SESSAO ? TELA_OUVINDO : TELA_ESPERANDO);
    if (!ws_ok) cor(16, 0, 0);                  // vermelho: sem servidor
    else if (estado == SESSAO) cor(0, 24, 4);   // verde: ouvindo
    else cor(0, 0, 6);                          // azul fraco: esperando a palavra
}

// ---------- saída: fila → volume → I2S0, guardando a referência do AEC ----------
static void tocar(const void *pcm, size_t n)
{
    xSemaphoreTake(mtx_play, portMAX_DELAY);
    xStreamBufferSend(fila_play, pcm, n, 0);
    xSemaphoreGive(mtx_play);
}

static void calar(void)
{
    xSemaphoreTake(mtx_play, portMAX_DELAY);
    xStreamBufferReset(fila_play);              // ninguém fica bloqueado nela
    xSemaphoreGive(mtx_play);
}

// bipe curto de dois tons (subindo = ouvindo, descendo = parou). Passa pela fila,
// então entra na referência e o AEC tira do mic como o resto.
static void bipe(float f1, float f2)
{
    static int16_t b[TAXA * 140 / 1000];
    const int n = sizeof(b) / sizeof(b[0]), meio = n / 2, rampa = TAXA * 5 / 1000;
    for (int i = 0; i < n; i++) {
        float f = i < meio ? f1 : f2, env = 1.0f;
        int j = i < meio ? i : i - meio, len = meio;
        if (j < rampa) env = (float)j / rampa;
        else if (j > len - rampa) env = (float)(len - j) / rampa;
        b[i] = (int16_t)(7000.0f * env * sinf(2.0f * (float)M_PI * f * i / TAXA));
    }
    tocar(b, sizeof(b));
}
#define BIPE_INICIO() bipe(880, 1320)
#define BIPE_FIM()    bipe(1320, 880)
#define BIPE_ERRO()   bipe(330, 330)

// economia: o MAX98357A fica desligado (SD em 0) sem som; liga ao chegar áudio e
// desliga 2 s depois do último. Ligado (SD em 3,3 V) ele toca o canal esquerdo, e a
// voz vai igual nos dois.
static void amp(bool ligar)
{
    static int atual = -1;
    if (CONFIG_RLIGHT_AMP_SD_GPIO < 0 || atual == ligar) return;
    gpio_set_level(CONFIG_RLIGHT_AMP_SD_GPIO, ligar);
    atual = ligar;
    tela_falando(ligar);
}

static void tarefa_play(void *arg)
{
    int16_t buf[PLAY_AMOSTRAS];
    size_t escrito;
    int64_t ultimo_som = 0;
    for (;;) {
        size_t n = xStreamBufferReceive(fila_play, buf, sizeof(buf), 0);
        if (n) {
            ultimo_som = esp_timer_get_time();
            amp(true);
        } else if (esp_timer_get_time() - ultimo_som > 2000000) {
            amp(false);
        }
        if (n < sizeof(buf)) memset((uint8_t *)buf + n, 0, sizeof(buf) - n);
        for (int i = 0; i < PLAY_AMOSTRAS; i++) buf[i] = buf[i] * CONFIG_RLIGHT_VOLUME / 100;
        xStreamBufferSend(fila_ref, buf, sizeof(buf), 0);   // exatamente o que vai pro DAC
        i2s_channel_write(tx, buf, sizeof(buf), &escrito, portMAX_DELAY);   // dá o ritmo
    }
}

// ---------- entrada: INMP441 + referência → AFE ----------
static void tarefa_feed(void *arg)
{
    const int n = afe->get_feed_chunksize(afe_dados);
    const int canais = afe->get_feed_channel_num(afe_dados);   // "MR" = 2
    int32_t *bruto = heap_caps_malloc(n * sizeof(int32_t), MALLOC_CAP_INTERNAL);
    int16_t *ref = heap_caps_malloc(n * sizeof(int16_t), MALLOC_CAP_INTERNAL);
    int16_t *feed = heap_caps_malloc(n * canais * sizeof(int16_t), MALLOC_CAP_INTERNAL);
    size_t lido;
    ESP_LOGI(TAG, "feed: %d amostras x %d canais", n, canais);
    for (;;) {
        i2s_channel_read(rx, bruto, n * sizeof(int32_t), &lido, portMAX_DELAY);
        // referência alinhada: não deixa acumular atraso (no máximo ~2 blocos na fila)
        while (xStreamBufferBytesAvailable(fila_ref) > 3 * n * sizeof(int16_t))
            xStreamBufferReceive(fila_ref, ref, n * sizeof(int16_t), 0);
        size_t r = xStreamBufferReceive(fila_ref, ref, n * sizeof(int16_t), 0);
        if (r < n * sizeof(int16_t)) memset((uint8_t *)ref + r, 0, n * sizeof(int16_t) - r);
        for (int i = 0; i < n; i++) {
            int32_t v = bruto[i] >> (16 - CONFIG_RLIGHT_MIC_GANHO_BITS);   // 24 bits úteis
            feed[i * canais] = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
            feed[i * canais + 1] = ref[i];
        }
        afe->feed(afe_dados, feed);
    }
}

// ---------- sessão ----------
static void enviar_json(const char *fmt, const char *a, const char *b)
{
    char msg[160];
    snprintf(msg, sizeof(msg), fmt, a, b);
    if (ws_ok) esp_websocket_client_send_text(ws, msg, strlen(msg), pdMS_TO_TICKS(200));
}

static void abrir_sessao(const char *origem)
{
    if (!ws_ok) {                                // sem servidor: avisa e continua esperando
        BIPE_ERRO();
        return;
    }
#if !CONFIG_RLIGHT_PALAVRA_LUMA
    afe->disable_wakenet(afe_dados);
#endif
    estado = SESSAO;
    esp_wifi_set_ps(WIFI_PS_NONE);              // conversa: latência mínima
    BIPE_INICIO();
    if (strcmp(origem, "agente") != 0)
        enviar_json("{\"tipo\":\"inicio\",\"origem\":\"%s\",\"palavra\":\"%s\"}", origem, palavra);
    ESP_LOGI(TAG, "sessão aberta (%s)", origem);
    cor_estado();
}

static void fechar_sessao(void)
{
    if (estado != SESSAO) return;
    estado = ESPERANDO;
#if CONFIG_RLIGHT_ECONOMIA_WIFI
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);         // esperando a palavra: rádio dorme entre beacons
#endif
#if CONFIG_RLIGHT_PALAVRA_LUMA
    luma_zerar();
#else
    afe->enable_wakenet(afe_dados);
#endif
    BIPE_FIM();
    ESP_LOGI(TAG, "sessão encerrada");
    cor_estado();
}

static void tarefa_fetch(void *arg)
{
    vad_state_t vad_ant = VAD_SILENCE;
    for (;;) {
        afe_fetch_result_t *res = afe->fetch(afe_dados);
        pedido_t p = pedido;
        pedido = NADA;
        if (p == FECHAR) fechar_sessao();
        if (p == ABRIR_AGENTE && estado == ESPERANDO) abrir_sessao("agente");
        if (!res || res->ret_value == ESP_FAIL) continue;

#if CONFIG_RLIGHT_PALAVRA_LUMA
        bool detectou = estado == ESPERANDO && luma_processar((const int16_t *)res->data, res->data_size / 2);
#else
        bool detectou = estado == ESPERANDO && res->wakeup_state == WAKENET_DETECTED;
#endif
        if (detectou) {
            ESP_LOGI(TAG, "palavra detectada: %s", palavra);
            abrir_sessao("palavra");
            vad_ant = VAD_SILENCE;
            continue;
        }
        if (estado != SESSAO) continue;
        if (res->vad_state != vad_ant) {        // mantém a sessão viva enquanto você fala
            vad_ant = res->vad_state;
            enviar_json("{\"tipo\":\"vad\",\"fala\":%s%s}", vad_ant == VAD_SPEECH ? "true" : "false", "");
        }
        if (ws_ok)
            esp_websocket_client_send_bin(ws, (const char *)res->data, res->data_size, pdMS_TO_TICKS(100));
    }
}

// ---------- WebSocket ----------
static void ws_evento(void *arg, esp_event_base_t base, int32_t id, void *dados)
{
    esp_websocket_event_data_t *d = dados;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ws_ok = true;
        enviar_json("{\"tipo\":\"ola\",\"id\":\"%s\",\"fw\":\"%s\"}", CONFIG_RLIGHT_ID, "0.1");
        ESP_LOGI(TAG, "servidor conectado");
        cor_estado();
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
        ws_ok = false;
        calar();
        pedido = FECHAR;
        cor_estado();
        break;
    case WEBSOCKET_EVENT_DATA:
        if (d->op_code == 0x2) {                // voz do RLight
            tocar(d->data_ptr, d->data_len);
        } else if (d->op_code == 0x1 && d->payload_offset == 0 && d->data_len == d->payload_len) {
            cJSON *j = cJSON_ParseWithLength(d->data_ptr, d->data_len);
            const char *tipo = cJSON_GetStringValue(cJSON_GetObjectItem(j, "tipo"));
            if (!tipo) {
            } else if (!strcmp(tipo, "interrompido")) {
                calar();                         // você falou por cima: corta na hora
            } else if (!strcmp(tipo, "fim")) {
                pedido = FECHAR;
            } else if (!strcmp(tipo, "sessao")) {
                pedido = ABRIR_AGENTE;           // resultado do agente em segundo plano
            } else if (!strcmp(tipo, "texto")) {  // transcrição para a OLED
                const char *quem = cJSON_GetStringValue(cJSON_GetObjectItem(j, "quem"));
                const char *t = cJSON_GetStringValue(cJSON_GetObjectItem(j, "t"));
                if (quem && t) tela_texto(quem[0], t);
            }
            cJSON_Delete(j);
        }
        break;
    }
}

// ---------- Wi-Fi ----------
static void wifi_evento(void *arg, esp_event_base_t base, int32_t id, void *dados)
{
    if (base == WIFI_EVENT && (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED)) {
        xEventGroupClearBits(eventos_wifi, BIT0);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(eventos_wifi, BIT0);
    }
}

static void iniciar_wifi(void)
{
    eventos_wifi = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&ic));
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_evento, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_evento, NULL);
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, CONFIG_RLIGHT_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_RLIGHT_WIFI_SENHA, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
#if CONFIG_RLIGHT_ECONOMIA_WIFI
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);         // começa esperando a palavra
#else
    esp_wifi_set_ps(WIFI_PS_NONE);
#endif
}

void rlight_enviar_bateria(float v_bat, float ma_saida, int pct)
{
    char msg[120];
    snprintf(msg, sizeof(msg), "{\"tipo\":\"bateria\",\"v\":%.3f,\"ma\":%.1f,\"pct\":%d}",
             v_bat, ma_saida, pct);
    if (ws_ok) esp_websocket_client_send_text(ws, msg, strlen(msg), pdMS_TO_TICKS(200));
}

// ---------- I2S ----------
static void iniciar_i2s(void)
{
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.dma_desc_num = 3;                        // buffer curto: referência perto do real
    cc.dma_frame_num = PLAY_AMOSTRAS;
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &tx, NULL));
    i2s_std_config_t sc = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(TAXA),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .mclk = I2S_GPIO_UNUSED, .bclk = SPK_BCLK, .ws = SPK_LRC,
                      .dout = SPK_DIN, .din = I2S_GPIO_UNUSED },
    };
    sc.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;  // mono nos dois lados: o MAX98357A toca (L+R)/2
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &sc));

    i2s_chan_config_t rc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&rc, NULL, &rx));
    i2s_std_config_t sr = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(TAXA),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .mclk = I2S_GPIO_UNUSED, .bclk = MIC_SCK, .ws = MIC_WS,
                      .dout = I2S_GPIO_UNUSED, .din = MIC_SD },
    };
#if CONFIG_RLIGHT_MIC_SLOT_DIREITO
    sr.slot_cfg.slot_mask = I2S_STD_SLOT_RIGHT;
#else
    sr.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
#endif
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx, &sr));
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
    ESP_ERROR_CHECK(i2s_channel_enable(rx));
}

// ---------- AFE (ESP-SR) ----------
static void iniciar_afe(void)
{
    srmodel_list_t *modelos = esp_srmodel_init("model");
    afe_config_t *cfg = afe_config_init("MR", modelos, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
    cfg->aec_init = true;
#if CONFIG_RLIGHT_AEC_FD
    cfg->aec_mode = AEC_MODE_FD_LOW_COST;   // recomendado pela Espressif para conversa full-duplex
#endif
    cfg->vad_init = true;
#if CONFIG_RLIGHT_PALAVRA_LUMA
    cfg->wakenet_init = false;                  // "Ei, Luma" roda no microWakeWord (luma.cc)
    palavra = "ei_luma";
#else
    cfg->wakenet_init = true;
#endif
    cfg->afe_perferred_core = 1;
    cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    if (cfg->wakenet_init && cfg->wakenet_model_name) palavra = cfg->wakenet_model_name;
    afe_config_print(cfg);
    afe = esp_afe_handle_from_config(cfg);
    afe_dados = afe->create_from_config(cfg);
#if CONFIG_RLIGHT_PALAVRA_LUMA
    if (!luma_iniciar()) {
        ESP_LOGE(TAG, "falha ao carregar ei_luma.tflite");
        abort();
    }
#endif
    ESP_LOGI(TAG, "AFE pronto, palavra: %s", palavra);
}

void app_main(void)
{
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    if (CONFIG_RLIGHT_LED_GPIO >= 0) {
        led_strip_config_t lc = { .strip_gpio_num = CONFIG_RLIGHT_LED_GPIO, .max_leds = 1,
                                  .led_pixel_format = LED_PIXEL_FORMAT_GRB, .led_model = LED_MODEL_WS2812 };
        led_strip_rmt_config_t rc = { .resolution_hz = 10 * 1000 * 1000 };
        if (led_strip_new_rmt_device(&lc, &rc, &led) != ESP_OK) led = NULL;
    }
    cor(16, 0, 0);
    if (CONFIG_RLIGHT_AMP_SD_GPIO >= 0) {
        gpio_hold_dis(CONFIG_RLIGHT_AMP_SD_GPIO);   // vinha segurado do sono por bateria fraca
        gpio_reset_pin(CONFIG_RLIGHT_AMP_SD_GPIO);
        gpio_set_direction(CONFIG_RLIGHT_AMP_SD_GPIO, GPIO_MODE_OUTPUT);
        gpio_set_level(CONFIG_RLIGHT_AMP_SD_GPIO, 0);
    }

    fila_play = xStreamBufferCreateWithCaps(FILA_PLAY, 1, MALLOC_CAP_SPIRAM);
    fila_ref = xStreamBufferCreate(FILA_REF, 1);
    mtx_play = xSemaphoreCreateMutex();

    iniciar_i2s();
    iniciar_afe();
    tela_iniciar(strcmp(palavra, "ei_luma") ? "Hi ESP" : "Ei, Luma");
    xTaskCreatePinnedToCore(tarefa_play, "play", 4096, NULL, 7, NULL, 0);
    xTaskCreatePinnedToCore(tarefa_feed, "feed", 8192, NULL, 6, NULL, 0);
    xTaskCreatePinnedToCore(tarefa_fetch, "fetch", 8192, NULL, 5, NULL, 1);

    iniciar_wifi();
    xEventGroupWaitBits(eventos_wifi, BIT0, pdFALSE, pdTRUE, portMAX_DELAY);
    static char uri[192];
    snprintf(uri, sizeof(uri), "%s?token=%s", CONFIG_RLIGHT_SERVIDOR, CONFIG_RLIGHT_TOKEN);
    esp_websocket_client_config_t wc = {
        .uri = uri,
        .buffer_size = 4096,
        .task_stack = 6144,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms = 10000,
        .ping_interval_sec = 10,
    };
    ws = esp_websocket_client_init(&wc);
    esp_websocket_register_events(ws, WEBSOCKET_EVENT_ANY, ws_evento, NULL);
    esp_websocket_client_start(ws);
}
