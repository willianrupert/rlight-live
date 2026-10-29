// "Ei, Luma" com microWakeWord: frontend de espectrograma do TFLite Micro (os mesmos
// parâmetros do treino) + modelo de streaming quantizado. Mesma lógica do componente
// micro_wake_word do ESPHome, enxugada para um modelo só.
#include "luma.h"

#include <string.h>
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#include "frontend.h"        // esphome/esp-micro-speech-features
#include "frontend_util.h"
#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/schema/schema_generated.h"

static const char *TAG = "luma";
extern const uint8_t modelo[] asm("_binary_ei_luma_tflite_start");

static const int FEATURES = 40;
static const int SLICES_ANTES = 74;                 // ~0,74 s de áudio antes de poder detectar
static FrontendState fe;
static tflite::MicroInterpreter *interp;
static int passo, stride, ignorar;
static uint8_t janela[CONFIG_RLIGHT_LUMA_JANELA];
static int idx;
static int64_t us_total, n_inf;                   // custo medido, para avaliar CPU junto do AFE

bool luma_iniciar(void)
{
    FrontendConfig c;
    FrontendFillConfigWithDefaults(&c);
    c.window.size_ms = 30;
    c.window.step_size_ms = 10;
    c.filterbank.num_channels = FEATURES;
    c.filterbank.lower_band_limit = 125.0;
    c.filterbank.upper_band_limit = 7500.0;
    c.noise_reduction.smoothing_bits = 10;
    c.noise_reduction.even_smoothing = 0.025;
    c.noise_reduction.odd_smoothing = 0.06;
    c.noise_reduction.min_signal_remaining = 0.05;
    c.pcan_gain_control.enable_pcan = 1;
    c.pcan_gain_control.strength = 0.95;
    c.pcan_gain_control.offset = 80.0;
    c.pcan_gain_control.gain_bits = 21;
    c.log_scale.enable_log = 1;
    c.log_scale.scale_shift = 6;
    if (!FrontendPopulateState(&c, &fe, 16000)) return false;

    const tflite::Model *m = tflite::GetModel(modelo);
    if (m->version() != TFLITE_SCHEMA_VERSION) return false;
    static tflite::MicroMutableOpResolver<20> ops;
    ops.AddCallOnce(); ops.AddVarHandle(); ops.AddReshape(); ops.AddReadVariable();
    ops.AddStridedSlice(); ops.AddConcatenation(); ops.AddAssignVariable(); ops.AddConv2D();
    ops.AddMul(); ops.AddAdd(); ops.AddMean(); ops.AddFullyConnected(); ops.AddLogistic();
    ops.AddQuantize(); ops.AddDepthwiseConv2D(); ops.AddAveragePool2D(); ops.AddMaxPool2D();
    ops.AddPad(); ops.AddPack(); ops.AddSplitV();

    uint8_t *vars = (uint8_t *)heap_caps_malloc(1024, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint8_t *arena = (uint8_t *)heap_caps_malloc(CONFIG_RLIGHT_LUMA_ARENA, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!vars || !arena) return false;
    tflite::MicroAllocator *ma = tflite::MicroAllocator::Create(vars, 1024);
    tflite::MicroResourceVariables *mrv = tflite::MicroResourceVariables::Create(ma, 20);
    interp = new tflite::MicroInterpreter(m, ops, arena, CONFIG_RLIGHT_LUMA_ARENA, mrv);
    if (interp->AllocateTensors() != kTfLiteOk) {
        ESP_LOGE(TAG, "arena pequena demais (CONFIG_RLIGHT_LUMA_ARENA)");
        return false;
    }
    TfLiteTensor *in = interp->input(0);
    if (in->dims->size != 3 || in->dims->data[2] != FEATURES) return false;
    stride = in->dims->data[1];
    ESP_LOGI(TAG, "modelo pronto: stride %d, arena usada %u bytes", stride,
             (unsigned)interp->arena_used_bytes());
    luma_zerar();
    return true;
}

void luma_zerar(void)
{
    memset(janela, 0, sizeof(janela));
    ignorar = -SLICES_ANTES;
}

bool luma_processar(const int16_t *pcm, int n)
{
    bool achou = false;
    while (n > 0) {
        size_t lidas = 0;
        FrontendOutput o = FrontendProcessSamples(&fe, pcm, n, &lidas);
        pcm += lidas;
        n -= lidas;
        if (o.size != FEATURES) continue;
        int8_t *dst = interp->input(0)->data.int8 + FEATURES * passo;
        for (int i = 0; i < FEATURES; i++) {      // mesma escala do treino (ver ESPHome)
            int32_t v = ((o.values[i] * 256) + 333) / 666 - 128;
            dst[i] = v < -128 ? -128 : v > 127 ? 127 : v;
        }
        if (++ignorar > 0) ignorar = 0;
        if (++passo < stride) continue;
        passo = 0;
        int64_t t0 = esp_timer_get_time();
        if (interp->Invoke() != kTfLiteOk) continue;
        us_total += esp_timer_get_time() - t0;
        if (++n_inf == 1000) {                    // ~30 s: tempo médio e % de um núcleo
            ESP_LOGI(TAG, "inferência média %.2f ms a cada %d ms (%.1f%% de um núcleo)",
                     us_total / 1000.0 / n_inf, stride * 10, 100.0 * us_total / n_inf / (stride * 10000.0));
            us_total = n_inf = 0;
        }
        janela[idx = (idx + 1) % CONFIG_RLIGHT_LUMA_JANELA] = interp->output(0)->data.uint8[0];
        int soma = 0;
        for (int i = 0; i < CONFIG_RLIGHT_LUMA_JANELA; i++) soma += janela[i];
        if (ignorar == 0 && soma > CONFIG_RLIGHT_LUMA_LIMIAR * CONFIG_RLIGHT_LUMA_JANELA) {
            ESP_LOGI(TAG, "Ei, Luma! (média %d)", soma / CONFIG_RLIGHT_LUMA_JANELA);
            luma_zerar();
            achou = true;
        }
    }
    return achou;
}
