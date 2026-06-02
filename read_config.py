# -*- coding: utf-8 -*-
"""GRBL の設定値($$)とビルド情報($I)を読み出す。"""
import serial, time

SERIAL_PORT = 'COM3'
BAUD_RATE = 115200

s = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
time.sleep(2)
s.reset_input_buffer()

for cmd in ['$I', '$$']:
    print(f'\n===== {cmd} =====')
    s.write((cmd + '\n').encode())
    time.sleep(0.5)
    while True:
        line = s.readline().decode(errors='ignore').strip()
        if not line:
            break
        print(line)

s.close()
