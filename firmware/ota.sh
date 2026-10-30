#!/bin/bash
# Atualiza o terminal pelo Wi-Fi (OTA), sem cabo:
#   RLIGHT_SSH=usuario@servidor firmware/ota.sh               compila, envia e atualiza
#   RLIGHT_SSH=usuario@servidor firmware/ota.sh --sem-build   usa o build/ que já existe
# Sem RLIGHT_SSH, considera que o servidor roda nesta mesma máquina.
# O .bin vai para RLIGHT_FIRMWARE no servidor (o mesmo do config.env) e o POST /ota manda o
# terminal baixar de http://<servidor>:8765/firmware.bin, gravar na outra partição e
# reiniciar. Se o firmware novo não conectar no servidor em 3 min, volta sozinho ao
# anterior. Só o app vai pelo Wi-Fi: partitions.csv, bootloader e modelos do ESP-SR
# ainda exigem a USB.
set -e
cd "$(dirname "$0")"
DEST="${RLIGHT_FIRMWARE:-/var/lib/rlight-live/rlight_terminal.bin}"
PORTA="${RLIGHT_PORT:-8765}"
no_servidor() { if [ -n "$RLIGHT_SSH" ]; then ssh "$RLIGHT_SSH" "$1"; else bash -c "$1"; fi; }

if [ "$1" != "--sem-build" ]; then
  echo "[1/3] Compilando"
  docker run --rm -v "$PWD":/project -w /project espressif/idf:v5.4.2 idf.py build \
    | grep -E " error|Project build complete"
fi
BIN=build/rlight_terminal.bin
echo "[2/3] Enviando $(( $(wc -c < "$BIN") / 1024 )) KB para ${RLIGHT_SSH:-esta máquina}"
no_servidor "mkdir -p $(dirname "$DEST") && cat > $DEST.tmp && mv $DEST.tmp $DEST" < "$BIN"

echo "[3/3] Pedindo o OTA ao terminal"
no_servidor "curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:$PORTA/ota"
echo
