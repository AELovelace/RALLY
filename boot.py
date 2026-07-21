import network
import os
import sys
import time

try:
    from machine import Pin, reset_cause
except ImportError:
    Pin = None
    reset_cause = None

# 1. Define your Wi-Fi and static IP settings
WIFI_SSID = 'TailBoard'
WIFI_PASS = 'esp32router'

# IP, Subnet Mask, Gateway, DNS
STATIC_IP = ('192.168.44.3', '255.255.255.0', '192.168.44.1', '1.1.1.1')

# Hold this pin low during boot to skip the web UI and start WebREPL.
SAFE_MODE_PIN = 0
STARTUP_SETTLE_S = 1.5
WIFI_CONNECT_ATTEMPTS = 2
WIFI_CONNECT_TIMEOUT_S = 12
WIFI_RETRY_DELAY_S = 1.5
BOOT_FAILURE_FILE = 'microshell_boot_failures.txt'
BOOT_FAILURE_LIMIT = 3


def load_boot_failures():
    try:
        with open(BOOT_FAILURE_FILE, 'r') as handle:
            return int(handle.read().strip() or '0')
    except Exception:
        return 0


def store_boot_failures(count):
    try:
        with open(BOOT_FAILURE_FILE, 'w') as handle:
            handle.write(str(count))
    except Exception:
        pass


def print_reset_cause():
    if reset_cause is None:
        return

    try:
        cause = reset_cause()
    except Exception as exc:
        print('Unable to read reset cause:', exc)
        return

    print('Reset cause:', cause)

def connect_static():
    wlan = network.WLAN(network.STA_IF)

    for attempt in range(1, WIFI_CONNECT_ATTEMPTS + 1):
        wlan.active(False)
        time.sleep(0.5)
        wlan.active(True)
        time.sleep(0.75)

        if wlan.isconnected():
            break

        print('Connecting to network (attempt %s/%s)...' % (attempt, WIFI_CONNECT_ATTEMPTS))

        wlan.ifconfig(STATIC_IP)
        wlan.connect(WIFI_SSID, WIFI_PASS)

        timeout = WIFI_CONNECT_TIMEOUT_S
        while timeout > 0:
            if wlan.isconnected():
                break
            timeout -= 1
            time.sleep(1)

        if wlan.isconnected():
            break

        try:
            wlan.disconnect()
        except Exception:
            pass

        if attempt < WIFI_CONNECT_ATTEMPTS:
            time.sleep(WIFI_RETRY_DELAY_S)

    if wlan.isconnected():
        print('Network config:', wlan.ifconfig())
    else:
        print('Failed to connect. Re-trying with a hardware reset might help.')


def should_start_webrepl():
    if Pin is None:
        return False

    try:
        safe_mode = Pin(SAFE_MODE_PIN, Pin.IN, Pin.PULL_UP)
        time.sleep(0.05)
        return safe_mode.value() == 0
    except Exception as exc:
        print('Safe mode check failed:', exc)
        return False


def start_webrepl(reason):
    import webrepl
    print(reason)
    webrepl.start()


time.sleep(STARTUP_SETTLE_S)
print_reset_cause()

connect_static()

if should_start_webrepl():
    start_webrepl('Safe mode enabled, starting WebREPL.')
else:
    failures = load_boot_failures()
    if failures >= BOOT_FAILURE_LIMIT:
        start_webrepl('MicroShell failed repeatedly, starting WebREPL fallback.')
        raise SystemExit

    print('Starting MicroShell web UI from main.py.')
    try:
        import main
    except Exception as exc:
        store_boot_failures(failures + 1)
        print('Failed to import main.py:', exc)
        if hasattr(sys, 'print_exception'):
            sys.print_exception(exc)
        start_webrepl('MicroShell import failed, starting WebREPL fallback.')