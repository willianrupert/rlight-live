#!/usr/bin/env python3
"""Gera treinar_ei_luma.ipynb (Colab) a partir das células abaixo. Baseado no
basic_training_notebook.ipynb do microWakeWord (kahrendt), adaptado para "Ei, Luma"."""
import json
from pathlib import Path

C = []
md = lambda s: C.append(("markdown", s))
py = lambda s: C.append(("code", s))

md("""# 🎙️ "Ei, Luma" — treino do microWakeWord (RLight Live)

Treina a palavra de ativação do terminal ESP32-S3 do RLight Live (`firmware`).
Baseado no notebook oficial do [microWakeWord](https://github.com/kahrendt/microWakeWord).

**Antes:** *Ambiente de execução → Alterar o tipo → GPU (T4)*. Rode célula por célula.
Leva ~1–2 h no Colab (a maior parte no treino). No fim baixa `ei_luma.tflite`:
copie para `firmware/main/palavra/ei_luma.tflite` e recompile o firmware.

**Amostras positivas (sintéticas):**
- Piper LibriTTS-R (~900 vozes em inglês) dizendo *"hey luma"* — soa quase igual a "Ei, Luma".
- Vozes Piper pt-BR (faber, cadu, jeff, edresson) dizendo *"Ei, Luma"*.
- Edge TTS pt-BR + vozes multilíngues (sotaques variados), com velocidade e tom variando.

**Negativas parecidas** (o que mais causa disparo falso): "Ei, Luna", "Ei, Lula", "pluma", "uma"…""")

py("""# 1) Instala o microWakeWord. Depois desta célula: Ambiente de execução → Reiniciar sessão.
!pip install -q 'git+https://github.com/whatsnowplaying/audio-metadata@d4ebb238e6a401bb1a5aaaac60c9e2b3cb30929f'
!git clone -q https://github.com/kahrendt/microWakeWord
!pip install -q -e ./microWakeWord
!pip install -q piper-sample-generator edge-tts
!apt-get -qq install -y ffmpeg > /dev/null""")

py("""# 2) Vozes: gerador LibriTTS-R (inglês, multi-locutor) + vozes Piper pt-BR
import os
os.makedirs('vozes', exist_ok=True)
!wget -q -nc -O vozes/en_US-libritts_r-medium.pt 'https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt'
HF = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR'
PT = [('faber', 'medium'), ('cadu', 'medium'), ('jeff', 'medium'), ('edresson', 'low')]
for nome, q in PT:
    for ext in ('onnx', 'onnx.json'):
        !wget -q -nc -O vozes/pt_BR-{nome}-{q}.{ext} '{HF}/{nome}/{q}/pt_BR-{nome}-{q}.{ext}?download=true'
VOZES_PT = ' '.join(f'--model vozes/pt_BR-{n}-{q}.onnx' for n, q in PT)""")

py("""# 3) Ouça uma amostra de cada fonte antes de gerar milhares
from IPython.display import Audio, display
!python3 -m piper_sample_generator 'hey luma' --model vozes/en_US-libritts_r-medium.pt --max-samples 2 --output-dir teste_en
!python3 -m piper_sample_generator 'Ei, Luma!' {VOZES_PT} --max-samples 4 --output-dir teste_pt
for f in ['teste_en/0.wav', 'teste_en/1.wav', 'teste_pt/0.wav', 'teste_pt/2.wav']:
    display(Audio(f))""")

py("""# 4) Gera as POSITIVAS. Tudo vira WAV 16 kHz mono em positivas/ (nomes únicos por fonte).
import asyncio, glob, random, shutil, subprocess
import edge_tts

def juntar(pasta, prefixo, destino):
    os.makedirs(destino, exist_ok=True)
    for i, f in enumerate(sorted(glob.glob(f'{pasta}/*.wav'))):
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', f, '-ar', '16000', '-ac', '1',
                        f'{destino}/{prefixo}_{i:05d}.wav'], check=True)

# inglês, muitos locutores ("hey" ≈ "ei"); grafias diferentes variam a pronúncia
for i, txt in enumerate(['hey luma', 'hey looma', 'hey, luma!']):
    !python3 -m piper_sample_generator '{txt}' --model vozes/en_US-libritts_r-medium.pt --max-samples 700 --batch-size 100 --max-speakers 800 --output-dir bruto/en_{i}
    juntar(f'bruto/en_{i}', f'en{i}', 'positivas')

# pt-BR (poucas vozes, então varia velocidade e ruído do modelo)
for i, txt in enumerate(['Ei, Luma!', 'Ei, Luma.', 'Ei, Luma?', 'ei luma']):
    !python3 -m piper_sample_generator '{txt}' {VOZES_PT} --max-samples 200 --length-scales 0.8 0.9 1.0 1.15 1.3 --noise-scales 0.5 0.667 0.9 --output-dir bruto/pt_{i}
    juntar(f'bruto/pt_{i}', f'pt{i}', 'positivas')

EDGE = ['pt-BR-FranciscaNeural', 'pt-BR-AntonioNeural', 'pt-BR-ThalitaMultilingualNeural',
        'en-US-AvaMultilingualNeural', 'en-US-AndrewMultilingualNeural', 'en-US-EmmaMultilingualNeural',
        'en-US-BrianMultilingualNeural', 'fr-FR-VivienneMultilingualNeural', 'de-DE-FlorianMultilingualNeural',
        'pt-PT-RaquelNeural', 'pt-PT-DuarteNeural']

async def edge(textos, pasta, n):
    os.makedirs(pasta, exist_ok=True)
    for k in range(n):
        voz, txt = random.choice(EDGE), random.choice(textos)
        rate, pitch = f'{random.randint(-25, 25):+d}%', f'{random.randint(-15, 15):+d}Hz'
        mp3 = f'{pasta}/{k:05d}.mp3'
        try:
            await edge_tts.Communicate(txt, voz, rate=rate, pitch=pitch).save(mp3)
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', mp3, '-ar', '16000', '-ac', '1',
                            mp3[:-4] + '.wav'], check=True)
        except Exception as e:
            print('edge falhou:', voz, e)

await edge(['Ei, Luma!', 'Ei, Luma.', 'Ei, Luma?', 'Ei Luma', 'Êi, Luma!'], 'bruto/edge', 600)
juntar('bruto/edge', 'edge', 'positivas')
print(len(glob.glob('positivas/*.wav')), 'positivas')""")

py("""# 5) Gera as NEGATIVAS PARECIDAS (mesmas fontes, frases que soam perto de "Ei, Luma")
PARECIDAS_PT = ['Ei, Luna!', 'Ei, Lula.', 'Ei, lua!', 'uma', 'pluma', 'Ei, Lúcia!', 'Ei, Laura!',
                'Ei, Luiza!', 'a Luma', 'luminária', 'Ei, você!', 'Ei, rua', 'lume', 'olha a lama',
                'Ei, mãe!', 'é uma', 'Ei, Lucas!', 'lá em cima', 'Ei, Lurdes!', 'Ei, menina']
await edge(PARECIDAS_PT, 'bruto/neg_edge', 900)
juntar('bruto/neg_edge', 'nedge', 'negativas')
for i, txt in enumerate(['Ei, Luna!', 'Ei, Lula.', 'uma pluma', 'Ei, Lúcia!']):
    !python3 -m piper_sample_generator '{txt}' {VOZES_PT} --max-samples 80 --length-scales 0.9 1.0 1.2 --output-dir bruto/npt_{i}
    juntar(f'bruto/npt_{i}', f'npt{i}', 'negativas')
for i, txt in enumerate(['hey luna', 'hey lumen', 'hey you', 'hello']):
    !python3 -m piper_sample_generator '{txt}' --model vozes/en_US-libritts_r-medium.pt --max-samples 200 --batch-size 100 --max-speakers 800 --output-dir bruto/nen_{i}
    juntar(f'bruto/nen_{i}', f'nen{i}', 'negativas')
print(len(glob.glob('negativas/*.wav')), 'negativas parecidas')""")

py("""# 6) Ruído, reverberação e música para augmentação (igual ao notebook oficial; demora)
import datasets, scipy
import numpy as np
from pathlib import Path
from tqdm import tqdm

if not os.path.exists('mit_rirs'):
    os.mkdir('mit_rirs')
    for row in tqdm(datasets.load_dataset('davidscripka/MIT_environmental_impulse_responses', split='train', streaming=True)):
        scipy.io.wavfile.write(os.path.join('mit_rirs', row['audio']['path'].split('/')[-1]), 16000,
                               (row['audio']['array'] * 32767).astype(np.int16))

if not os.path.exists('audioset_16k'):
    os.makedirs('audioset', exist_ok=True)
    !wget -q -O audioset/bal_train09.tar https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train09.tar
    !cd audioset && tar -xf bal_train09.tar
    os.mkdir('audioset_16k')
    ds = datasets.Dataset.from_dict({'audio': [str(i) for i in Path('audioset/audio').glob('**/*.flac')]})
    for row in tqdm(ds.cast_column('audio', datasets.Audio(sampling_rate=16000))):
        scipy.io.wavfile.write(os.path.join('audioset_16k', row['audio']['path'].split('/')[-1].replace('.flac', '.wav')),
                               16000, (row['audio']['array'] * 32767).astype(np.int16))

if not os.path.exists('fma_16k'):
    os.makedirs('fma', exist_ok=True)
    !wget -q -O fma/fma_xs.zip https://huggingface.co/datasets/mchl914/fma_xsmall/resolve/main/fma_xs.zip
    !cd fma && unzip -q fma_xs.zip
    os.mkdir('fma_16k')
    ds = datasets.Dataset.from_dict({'audio': [str(i) for i in Path('fma/fma_small').glob('**/*.mp3')]})
    for row in tqdm(ds.cast_column('audio', datasets.Audio(sampling_rate=16000))):
        scipy.io.wavfile.write(os.path.join('fma_16k', row['audio']['path'].split('/')[-1].replace('.mp3', '.wav')),
                               16000, (row['audio']['array'] * 32767).astype(np.int16))""")

py("""# 7) Augmenta e extrai os espectrogramas (positivas e negativas parecidas)
from microwakeword.audio.augmentation import Augmentation
from microwakeword.audio.clips import Clips
from microwakeword.audio.spectrograms import SpectrogramGeneration
from mmap_ninja.ragged import RaggedMmap

augmenter = Augmentation(augmentation_duration_s=3.2,
                         augmentation_probabilities={'SevenBandParametricEQ': 0.1, 'TanhDistortion': 0.1,
                                                     'PitchShift': 0.1, 'BandStopFilter': 0.1,
                                                     'AddColorNoise': 0.1, 'AddBackgroundNoise': 0.75,
                                                     'Gain': 1.0, 'RIR': 0.5},
                         impulse_paths=['mit_rirs'], background_paths=['fma_16k', 'audioset_16k'],
                         background_min_snr_db=-5, background_max_snr_db=10,
                         min_jitter_s=0.195, max_jitter_s=0.205)

def features(pasta_wav, saida):
    clips = Clips(input_directory=pasta_wav, file_pattern='*.wav', max_clip_duration_s=None,
                  remove_silence=False, random_split_seed=10, split_count=0.1)
    for split, nome, rep, slide in [('training', 'train', 2, 10), ('validation', 'validation', 1, 10),
                                    ('testing', 'test', 1, 1)]:
        os.makedirs(f'{saida}/{split}', exist_ok=True)
        sg = SpectrogramGeneration(clips=clips, augmenter=augmenter, slide_frames=slide, step_ms=10)
        RaggedMmap.from_generator(out_dir=f'{saida}/{split}/wakeword_mmap',
                                  sample_generator=sg.spectrogram_generator(split=nome, repeat=rep),
                                  batch_size=100, verbose=True)

features('positivas', 'feat_positivas')
features('negativas', 'feat_negativas_parecidas')""")

py("""# 8) Negativas gerais pré-calculadas do microWakeWord (fala, festa, sem fala)
if not os.path.exists('negative_datasets'):
    os.mkdir('negative_datasets')
    for f in ['dinner_party.zip', 'dinner_party_eval.zip', 'no_speech.zip', 'speech.zip']:
        !wget -q -O negative_datasets/{f} https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main/{f}
        !unzip -q negative_datasets/{f} -d negative_datasets""")

py("""# 9) Configuração do treino
import yaml
neg = lambda d, w, trunc='random': {'features_dir': d, 'sampling_weight': w, 'penalty_weight': 1.0,
                                     'truth': False, 'truncation_strategy': trunc, 'type': 'mmap'}
config = {
    'window_step_ms': 10,
    'train_dir': 'trained_models/ei_luma',
    'features': [
        {'features_dir': 'feat_positivas', 'sampling_weight': 2.0, 'penalty_weight': 1.0,
         'truth': True, 'truncation_strategy': 'truncate_start', 'type': 'mmap'},
        neg('feat_negativas_parecidas', 4.0),
        neg('negative_datasets/speech', 10.0),
        neg('negative_datasets/dinner_party', 10.0),
        neg('negative_datasets/no_speech', 5.0),
        neg('negative_datasets/dinner_party_eval', 0.0, 'split'),
    ],
    'training_steps': [20000],
    'positive_class_weight': [1], 'negative_class_weight': [20],
    'learning_rates': [0.001], 'batch_size': 128,
    'time_mask_max_size': [0], 'time_mask_count': [0], 'freq_mask_max_size': [0], 'freq_mask_count': [0],
    'eval_step_interval': 500,
    'clip_duration_ms': 1500,
    'target_minimization': 0.9, 'minimization_metric': None,
    'maximization_metric': 'average_viable_recall',
}
yaml.dump(config, open('training_parameters.yaml', 'w'))""")

py("""# 10) Treina, quantiza e testa o modelo de streaming (o que roda no S3).
# No fim ele imprime recall e falsos positivos por hora para vários limiares: anote o
# limiar com < 0,5 FA/h e recall alto — ele vai em CONFIG_RLIGHT_LUMA_LIMIAR no firmware.
!python -m microwakeword.model_train_eval \\
--training_config='training_parameters.yaml' --train 1 --restore_checkpoint 1 \\
--test_tf_nonstreaming 0 --test_tflite_nonstreaming 0 --test_tflite_nonstreaming_quantized 0 \\
--test_tflite_streaming 0 --test_tflite_streaming_quantized 1 --use_weights 'best_weights' \\
mixednet --pointwise_filters '64,64,64,64' --repeat_in_block '1, 1, 1, 1' \\
--mixconv_kernel_sizes '[5], [7,11], [9,15], [23]' --residual_connection '0,0,0,0' \\
--first_conv_filters 32 --first_conv_kernel_size 5 --stride 3""")

py("""# 11) Baixa o modelo → firmware/main/palavra/ei_luma.tflite
import shutil
from google.colab import files
shutil.copy('trained_models/ei_luma/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite', 'ei_luma.tflite')
print(os.path.getsize('ei_luma.tflite'), 'bytes')
files.download('ei_luma.tflite')""")

nb = {"nbformat": 4, "nbformat_minor": 0,
      "metadata": {"accelerator": "GPU", "colab": {"provenance": [], "gpuType": "T4"},
                   "kernelspec": {"name": "python3", "display_name": "Python 3"}},
      "cells": [{"cell_type": t, "metadata": {}, "source": s.splitlines(True),
                 **({"outputs": [], "execution_count": None} if t == "code" else {})} for t, s in C]}
Path(__file__).with_name("treinar_ei_luma.ipynb").write_text(json.dumps(nb, ensure_ascii=False, indent=1))
print("ok:", len(C), "células")
