# -*- coding: utf-8 -*-
"""
ペン上下チェック（Z軸・絶対位置版）
------------------------------------------------
Z=0(最下) と Z=10(最上) を絶対座標で交互に作り、
どちらでペンが紙に着く/離れるかを確認する。

使い方:
  python pen_test.py
"""

import sys
import time

try:
    import serial
except ImportError:
    print("pyserial が入っていません: pip install pyserial")
    sys.exit(1)

SERIAL_PORT = 'COM3'
BAUD_RATE   = 115200


def send(ser, cmd, wait=0.0):
    ser.write((cmd + '\n').encode())
    resp = ser.readline().decode(errors='ignore').strip()
    print(f">>> {cmd:<14} | {resp}")
    if wait:
        time.sleep(wait)
    return resp


def status(ser):
    """現在位置(?)を表示"""
    ser.write(b'?')
    time.sleep(0.3)
    line = ser.readline().decode(errors='ignore').strip()
    print(f"    [状態] {line}")


def main():
    print(f"ポート {SERIAL_PORT} に接続...")
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    print("接続完了。ペン先が紙に着く/離れるのを見てください。\n")

    try:
        send(ser, 'G21')
        send(ser, 'G90')          # 絶対座標
        status(ser)

        print("\n--- Z=10（最上）へ ---")
        send(ser, 'G1 Z10 F200', wait=3)
        status(ser)

        print("\n--- Z=0（最下）へ ---")
        send(ser, 'G1 Z0 F200', wait=3)
        status(ser)

        print("\n--- もう一度 Z=10（最上）へ ---")
        send(ser, 'G1 Z10 F200', wait=3)
        status(ser)

        print("\n--- もう一度 Z=0（最下）へ ---")
        send(ser, 'G1 Z0 F200', wait=3)
        status(ser)

    finally:
        ser.close()
        print("\n接続を閉じました。")
        print("→ Z=0 と Z=10 のどちらでペンが紙に着いたか教えてください。")


if __name__ == '__main__':
    main()
