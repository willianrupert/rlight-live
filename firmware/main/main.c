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
#include "freertos/queue.h"
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
#include "esp_ota_ops.h"
#include "esp_http_client.h"
#include "esp_app_desc.h"
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
typedef enum { NADA, ABRIR_AGENTE, FECHAR, ATUALIZAR } pedido_t;

static i2s_chan_handle_t rx, tx;
static StreamBufferHandle_t fila_play, fila_ref, fila_envio;
static SemaphoreHandle_t mtx_play;
static esp_websocket_client_handle_t ws;
static const esp_afe_sr_iface_t *afe;
static esp_afe_sr_data_t *afe_dados;
static const char *palavra = "?";
static volatile estado_t estado = ESPERANDO;
static volatile pedido_t pedido = NADA;
static volatile bool atualizando = false;    // vem do WebSocket, aplicado na tarefa do AFE
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

static volatile int64_t tocou_em = 0;     // último bloco com voz no alto-falante

static void tarefa_play(void *arg)
{
    int16_t buf[PLAY_AMOSTRAS];
    size_t escrito;
    int64_t ultimo_som = 0;
    for (;;) {
        size_t n = xStreamBufferReceive(fila_play, buf, sizeof(buf), 0);
        if (n) {
            ultimo_som = esp_timer_get_time();
            tocou_em = ultimo_som;
            amp(true);
        } else if (esp_timer_get_time() - ultimo_som > 2000000) {
            amp(false);
        }
        if (n < sizeof(buf)) memset((uint8_t *)buf + n, 0, sizeof(buf) - n);
        for (int i = 0; i < PLAY_AMOSTRAS; i++) {
            float x = buf[i] * (CONFIG_RLIGHT_VOLUME / 100.0f);
#if CONFIG_RLIGHT_EQ
            // alto-falante pequeno: corta < 150 Hz (não reproduz, só distorce) e −4 dB acima de
            // ~3,5 kHz (agudo de lata); nada é reforçado, então não satura
            static float l1, l2, ly1, ly2, h1, h2, hy1, hy2;
            float y = 0.95920315f * x - 1.91840630f * l1 + 0.95920315f * l2 + 1.91674122f * ly1 - 0.92007138f * ly2;
            l2 = l1; l1 = x; ly2 = ly1; ly1 = y;
            x = 0.77356908f * y - 0.07469976f * h1 + 0.13399900f * h2 + 0.36196065f * hy1 - 0.19482897f * hy2;
            h2 = h1; h1 = y; hy2 = hy1; hy1 = x;
#endif
            buf[i] = x > 32767 ? 32767 : x < -32768 ? -32768 : (int16_t)x;
        }
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
    int64_t diag_t = esp_timer_get_time();
    float diag_soma = 0, diag_pico = 0;
    int32_t diag_n = 0, diag_zeros = 0, diag_cheios = 0;
    float x1 = 0, x2 = 0, y1 = 0, y2 = 0;            // passa-alta 100 Hz (Butterworth 2ª ordem)
    const float ganho = 1 << CONFIG_RLIGHT_MIC_GANHO_BITS;
    for (;;) {
        i2s_channel_read(rx, bruto, n * sizeof(int32_t), &lido, portMAX_DELAY);
        if (atualizando) continue;                // OTA rodando na tarefa do fetch: não enche o AFE
        // referência alinhada: não deixa acumular atraso (no máximo ~2 blocos na fila)
        while (xStreamBufferBytesAvailable(fila_ref) > 3 * n * sizeof(int16_t))
            xStreamBufferReceive(fila_ref, ref, n * sizeof(int16_t), 0);
        size_t r = xStreamBufferReceive(fila_ref, ref, n * sizeof(int16_t), 0);
        if (r < n * sizeof(int16_t)) memset((uint8_t *)ref + r, 0, n * sizeof(int16_t) - r);
        for (int i = 0; i < n; i++) {
            // o INMP441 traz um ronco < 100 Hz (ripple do boost) maior que a voz: corta antes do ganho
            float x = (bruto[i] >> 8) / 256.0f;       // 24 bits úteis, em escala de 16 bits
            float y = 0.9726139f * (x - 2 * x1 + x2) + 1.94447766f * y1 - 0.94597794f * y2;
            x2 = x1; x1 = x; y2 = y1; y1 = y;
            float v = y * ganho;
            feed[i * canais] = v > 32767 ? 32767 : v < -32768 ? -32768 : (int16_t)v;
            feed[i * canais + 1] = ref[i];
            // nível no log a cada 3 s: zeros = sem dado (fio/canal L/R), cheios = SD preso em 1
            if (bruto[i] == 0) diag_zeros++;
            if (bruto[i] == -1 || bruto[i] == (int32_t)0xFFFFFF00) diag_cheios++;
            if (fabsf(v) > diag_pico) diag_pico = fabsf(v);
            diag_soma += v * v;
        }
        diag_n += n;
#ifdef RLIGHT_DUMP_MIC
        // grava 6 s do mic cru (24 bits) na PSRAM a partir de 5 s do boot e despeja devagar pela serial
        static int32_t *gravacao = NULL;
        static int pos = 0;
        const int total = 16000 * 6;
        if (!gravacao) gravacao = heap_caps_malloc(total * sizeof(int32_t), MALLOC_CAP_SPIRAM);
        if (esp_timer_get_time() > 5000000 && pos < total) {
            for (int i = 0; i < n && pos < total; i++) gravacao[pos++] = bruto[i] >> 8;
            if (pos == total) {
                for (int i = 0; i < total; i += 32) {
                    printf("D:");
                    for (int k = 0; k < 32; k++) printf("%06x", (unsigned)(gravacao[i + k] & 0xFFFFFF));
                    printf("\n");
                    if (i % 512 == 0) vTaskDelay(1);
                }
                printf("FIM_DUMP\n");
            }
        }
#endif
        afe->feed(afe_dados, feed);
        if (esp_timer_get_time() - diag_t > 3000000) {
            ESP_LOGI(TAG, "mic (filtrado, ganho x%d): rms %d pico %d de 32767, zeros %d%% cheios %d%%",
                     (int)ganho, (int)sqrtf(diag_soma / diag_n), (int)diag_pico,
                     (int)(100LL * diag_zeros / diag_n), (int)(100LL * diag_cheios / diag_n));
            diag_t = esp_timer_get_time();
            diag_soma = diag_pico = 0;
            diag_n = diag_zeros = diag_cheios = 0;
        }
    }
}

// ---------- sessão ----------
typedef struct { char txt[160]; } msg_json_t;
static QueueHandle_t fila_json;

// só enfileira: quem escreve no socket é a tarefa_envio, para a rede lenta nunca travar o AFE
static void enviar_json(const char *fmt, const char *a, const char *b)
{
    msg_json_t m;
    snprintf(m.txt, sizeof(m.txt), fmt, a, b);
    xQueueSend(fila_json, &m, 0);
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

// ---------- OTA ----------
// O servidor manda {"tipo":"ota"} (firmware/ota.sh) e o terminal baixa o .bin do próprio
// servidor (http://…:8765/firmware.bin?token=…) para a outra partição de app. Roda aqui, na
// tarefa do fetch, porque gravar flash exige pilha na RAM interna e não sobra RAM para uma
// tarefa nova. O app novo só vira definitivo depois de conectar no servidor (ver
// confirmar_firmware); se travar ou não conectar em 3 min, o bootloader volta ao anterior.
static void atualizar_firmware(void)
{
    if (estado == SESSAO) fechar_sessao();
    atualizando = true;
    calar();
    cor(16, 0, 16);
    tela_texto('r', "Atualizando o firmware...");
    char url[200];
    const char *base = CONFIG_RLIGHT_SERVIDOR;                 // ws://host:porta/dispositivo
    const char *host = strstr(base, "://") ? strstr(base, "://") + 3 : base;
    int len_host = strcspn(host, "/");
    snprintf(url, sizeof(url), "http://%.*s/firmware.bin?token=%s", len_host, host, CONFIG_RLIGHT_TOKEN);

    const esp_partition_t *destino = esp_ota_get_next_update_partition(NULL);
    esp_http_client_config_t hc = { .url = url, .timeout_ms = 15000, .buffer_size = 2048 };
    esp_http_client_handle_t cli = esp_http_client_init(&hc);
    esp_ota_handle_t ota = 0;
    static uint8_t buf[2048];
    const char *erro = NULL;
    int total = 0;
    if (!destino) erro = "sem partição de OTA (grave uma vez pela USB com o partitions.csv novo)";
    else if (esp_http_client_open(cli, 0) != ESP_OK) erro = "não conectou no servidor";
    else if (esp_http_client_fetch_headers(cli) < 0 || esp_http_client_get_status_code(cli) != 200)
        erro = "servidor não entregou o firmware";
    else if (esp_ota_begin(destino, OTA_WITH_SEQUENTIAL_WRITES, &ota) != ESP_OK) erro = "esp_ota_begin falhou";
    while (!erro) {
        int n = esp_http_client_read(cli, (char *)buf, sizeof(buf));
        if (n < 0) erro = "download interrompido";
        else if (n == 0) break;
        else if (esp_ota_write(ota, buf, n) != ESP_OK) erro = "gravação falhou";
        else if ((total += n) % (256 * 1024) < n) ESP_LOGI(TAG, "OTA: %d KB", total / 1024);
    }
    if (!erro && !esp_http_client_is_complete_data_received(cli)) erro = "download incompleto";
    if (ota && !erro && esp_ota_end(ota) != ESP_OK) erro = "imagem inválida";
    else if (ota && erro) esp_ota_abort(ota);
    if (!erro && esp_ota_set_boot_partition(destino) != ESP_OK) erro = "não trocou a partição de boot";
    esp_http_client_cleanup(cli);
    if (erro) {
        ESP_LOGE(TAG, "OTA falhou: %s", erro);
        enviar_json("{\"tipo\":\"ota\",\"ok\":false,\"erro\":\"%s\"%s}", erro, "");
        tela_texto('r', erro);
        BIPE_ERRO();
        atualizando = false;
        cor_estado();
        return;
    }
    ESP_LOGI(TAG, "OTA ok: %d bytes em %s, reiniciando", total, destino->label);
    enviar_json("{\"tipo\":\"ota\",\"ok\":true,\"particao\":\"%s\"%s}", destino->label, "");
    vTaskDelay(pdMS_TO_TICKS(500));                  // deixa o aviso sair
    esp_restart();
}

// primeira conexão com o servidor depois de um OTA: firmware aprovado, cancela o rollback
static void confirmar_firmware(void)
{
    esp_ota_img_states_t st;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        esp_ota_mark_app_valid_cancel_rollback();
        ESP_LOGI(TAG, "firmware novo confirmado");
    }
}

static void vigia_rollback(void *arg)
{
    esp_ota_img_states_t st;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGW(TAG, "firmware novo sem servidor há 3 min: voltando ao anterior");
        esp_ota_mark_app_invalid_rollback_and_reboot();
    }
}

static void tarefa_fetch(void *arg)
{
    vad_state_t vad_ant = VAD_SILENCE;
    int64_t diag_t = esp_timer_get_time();
    int diag_n = 0, diag_voz = 0;
    float diag_vol = -200, diag_eco = -200;
    int fortes = 0;
    int64_t aberto_ate = 0;
    for (;;) {
        afe_fetch_result_t *res = afe->fetch(afe_dados);
        pedido_t p = pedido;
        pedido = NADA;
        if (p == FECHAR) fechar_sessao();
        if (p == ABRIR_AGENTE && estado == ESPERANDO) abrir_sessao("agente");
        if (p == ATUALIZAR) atualizar_firmware();
        if (!res || res->ret_value == ESP_FAIL) continue;
        // saída do AFE no log a cada 3 s: volume máximo e quanto do tempo o VAD achou voz
        diag_n++;
        if (res->vad_state == VAD_SPEECH) diag_voz++;
        if (res->data_volume > diag_vol) diag_vol = res->data_volume;
        if (esp_timer_get_time() - diag_t > 3000000) {
            ESP_LOGI(TAG, "afe: volume máx %.0f dBFS, voz %d%%, eco máx %.0f dBFS (portão %d)",
                     diag_vol, 100 * diag_voz / diag_n, diag_eco, CONFIG_RLIGHT_BARGEIN_DBFS);
            diag_eco = -200;
            diag_t = esp_timer_get_time();
            diag_n = diag_voz = 0;
            diag_vol = -200;
        }

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
        // portão de eco: com o alto-falante colado no mic, o AEC sozinho deixa passar a voz do
        // terminal e o Gemini responde a si mesmo em loop. Enquanto ele fala (e 300 ms depois),
        // só sobe áudio se o pós-AEC estiver bem acima do eco, ou seja, você falando por cima
        bool tocando = esp_timer_get_time() - tocou_em < 300000;
        if (tocando) {
            if (res->data_volume > diag_eco) diag_eco = res->data_volume;
            fortes = res->data_volume > CONFIG_RLIGHT_BARGEIN_DBFS ? fortes + 1 : 0;
            if (fortes >= 3) aberto_ate = esp_timer_get_time() + 600000;
        }
        if (tocando && esp_timer_get_time() > aberto_ate) {
            static int16_t zeros[1024];
            size_t nz = res->data_size < sizeof(zeros) ? res->data_size : sizeof(zeros);
            if (xStreamBufferSpacesAvailable(fila_envio) >= nz) xStreamBufferSend(fila_envio, zeros, nz, 0);
        } else {
            if (xStreamBufferSpacesAvailable(fila_envio) >= res->data_size)   // só blocos inteiros
                xStreamBufferSend(fila_envio, res->data, res->data_size, 0);   // cheia (Wi-Fi travado): descarta
        }
    }
}

// ---------- envio do mic ----------
// Separado do fetch: um engasgo do Wi-Fi não trava o AFE, e o timeout longo evita que o
// esp_websocket_client derrube a conexão (ele aborta em qualquer escrita que estoure o prazo)
static void tarefa_envio(void *arg)
{
    static uint8_t buf[2048];
    msg_json_t m;
    for (;;) {
        while (xQueueReceive(fila_json, &m, 0) == pdTRUE)
            if (ws_ok) esp_websocket_client_send_text(ws, m.txt, strlen(m.txt), pdMS_TO_TICKS(3000));
        size_t n = xStreamBufferReceive(fila_envio, buf, sizeof(buf), pdMS_TO_TICKS(20));
        if (n && ws_ok && estado == SESSAO)
            esp_websocket_client_send_bin(ws, (const char *)buf, n, pdMS_TO_TICKS(3000));
    }
}

// ---------- WebSocket ----------
static void ws_evento(void *arg, esp_event_base_t base, int32_t id, void *dados)
{
    esp_websocket_event_data_t *d = dados;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ws_ok = true;
        static char fw[40];
        snprintf(fw, sizeof(fw), "%s %s", esp_app_get_description()->date, esp_app_get_description()->time);
        enviar_json("{\"tipo\":\"ola\",\"id\":\"%s\",\"fw\":\"%s\"}", CONFIG_RLIGHT_ID, fw);
        ESP_LOGI(TAG, "servidor conectado");
        confirmar_firmware();
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
            } else if (!strcmp(tipo, "ota")) {
                pedido = ATUALIZAR;
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
    // o Hi ESP precisava de limiar 0,5 (padrão 0,63) com sotaque BR; o Alexa usa o padrão do modelo
    if (cfg->wakenet_init && strstr(palavra, "hiesp")) afe->set_wakenet_threshold(afe_dados, 1, 0.5);
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
    fila_json = xQueueCreate(8, sizeof(msg_json_t));
    fila_envio = xStreamBufferCreateWithCaps(64 * 1024, 1, MALLOC_CAP_SPIRAM);   // ~2 s de mic
    mtx_play = xSemaphoreCreateMutex();

    iniciar_i2s();
    iniciar_afe();
    tela_iniciar(strstr(palavra, "alexa") ? "Alexa" : strstr(palavra, "hiesp") ? "Hi ESP" : "Ei, Luma");
    xTaskCreatePinnedToCore(tarefa_play, "play", 4096, NULL, 7, NULL, 0);
    xTaskCreatePinnedToCore(tarefa_feed, "feed", 8192, NULL, 6, NULL, 0);
    xTaskCreatePinnedToCore(tarefa_fetch, "fetch", 8192, NULL, 5, NULL, 1);
    // pilha na PSRAM: a RAM interna é disputada pelo AFE, Wi-Fi e a tarefa do websocket
    xTaskCreatePinnedToCoreWithCaps(tarefa_envio, "envio", 4096, NULL, 4, NULL, 1, MALLOC_CAP_SPIRAM);

    const esp_timer_create_args_t ta = { .callback = vigia_rollback, .name = "rollback" };
    esp_timer_handle_t t_rollback;
    esp_timer_create(&ta, &t_rollback);
    esp_timer_start_once(t_rollback, 180 * 1000000LL);
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
    ESP_LOGI(TAG, "RAM interna livre: %u (maior bloco %u), PSRAM livre: %u de %u",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), (unsigned)heap_caps_get_total_size(MALLOC_CAP_SPIRAM));
    ws = esp_websocket_client_init(&wc);
    esp_websocket_register_events(ws, WEBSOCKET_EVENT_ANY, ws_evento, NULL);
    esp_websocket_client_start(ws);
}
