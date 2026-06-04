#!/usr/bin/env bash
# One-time setup for Waveshare ESP32-C6-LCD-1.47 stock ticker (MicroPython).
# Usage: edit PORT below, then  ./flash.sh
set -e

PORT="${1:-/dev/cu.usbmodem*}"   # pass port as arg or edit here

echo ">> Installing host tools (esptool + mpremote)"
pip install --quiet --upgrade esptool mpremote

echo ">> Downloading ESP32-C6 MicroPython firmware (latest stable)"
FW="ESP32_GENERIC_C6-20260406-v1.28.0.bin"
curl -L -o "$FW" "https://micropython.org/resources/firmware/$FW" || \
  echo "  (if 404, grab newest .bin from https://micropython.org/download/ESP32_GENERIC_C6/ )"

echo ">> Erasing + flashing firmware on $PORT"
esptool --chip esp32c6 --port "$PORT" erase-flash
esptool --chip esp32c6 --port "$PORT" --baud 460800 write-flash 0x0 "$FW"

sleep 3

echo ">> Fetching ST7789 pure-python driver + fonts from russhughes/st7789py_mpy"
BASE="https://raw.githubusercontent.com/russhughes/st7789py_mpy/master"
curl -L -o lib/st7789py.py        "$BASE/lib/st7789py.py"
curl -L -o lib/vga2_bold_16x32.py "$BASE/romfonts/vga2_bold_16x32.py"
curl -L -o lib/vga1_8x16.py       "$BASE/romfonts/vga1_8x16.py"

echo ">> Copying project files to the board"
mpremote connect "$PORT" fs cp config.json :config.json
mpremote connect "$PORT" fs cp main.py :main.py
mpremote connect "$PORT" fs mkdir lib || true
mpremote connect "$PORT" fs cp lib/st7789py.py        :lib/st7789py.py
mpremote connect "$PORT" fs cp lib/vga2_bold_16x32.py :lib/vga2_bold_16x32.py
mpremote connect "$PORT" fs cp lib/vga1_8x16.py       :lib/vga1_8x16.py

echo ">> Done. Reset the board. To edit config later:"
echo "   mpremote connect $PORT fs cp :config.json config.json   # pull"
echo "   mpremote connect $PORT fs cp config.json :config.json   # push"
echo "   mpremote connect $PORT repl                             # watch logs"
