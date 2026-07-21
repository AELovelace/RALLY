try:
    import ujson as json
except ImportError:
    import json

import gc
import os
import socket
import sys
import time

try:
    import neopixel
except ImportError:
    neopixel = None

try:
    import machine
except ImportError:
    machine = None

try:
    import network
except ImportError:
    network = None

HOST = '0.0.0.0'
PORT = 80
SCRIPTS_DIR = 'scripts'
WEB_ROOT = 'www'
AUTH_TOKEN = None
BOOT_FAILURE_FILE = 'microshell_boot_failures.txt'
STATUS_LED_PIN = 48

START_TICKS = time.ticks_ms()
EXEC_GLOBALS = {'__name__': '__main__'}
EXEC_DEPTH = 0


class OutputBuffer:
    def __init__(self):
        self.parts = []

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode('utf-8')
        self.parts.append(data)
        return len(data)

    def flush(self):
        return None

    def getvalue(self):
        return ''.join(self.parts)


def init_status_led():
    if machine is None or neopixel is None:
        return None

    try:
        led = neopixel.NeoPixel(machine.Pin(STATUS_LED_PIN), 1)
        led[0] = (0, 0, 0)
        led.write()
        return led
    except Exception:
        return None


def set_status_led(led, red, green, blue):
    if led is None:
        return

    try:
        led[0] = (red, green, blue)
        led.write()
    except Exception:
        pass


def ensure_scripts_dir():
    try:
        os.mkdir(SCRIPTS_DIR)
    except OSError:
        pass


def is_authorized(headers):
    if not AUTH_TOKEN:
        return True
    auth = headers.get('authorization', '')
    return auth == 'Bearer ' + AUTH_TOKEN


def sanitize_name(name):
    if not name:
        raise ValueError('Missing file name')
    if '/' in name or '\\' in name:
        raise ValueError('Nested paths are not allowed')
    if name.startswith('.'):
        raise ValueError('Hidden files are not allowed')
    for char in name:
        allowed = (
            ('a' <= char <= 'z') or
            ('A' <= char <= 'Z') or
            ('0' <= char <= '9') or
            char in ('-', '_', '.')
        )
        if not allowed:
            raise ValueError('Unsupported character in file name')
    return name


def url_decode(value):
    out = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == '%' and index + 2 < len(value):
            out.append(chr(int(value[index + 1:index + 3], 16)))
            index += 3
        elif char == '+':
            out.append(' ')
            index += 1
        else:
            out.append(char)
            index += 1
    return ''.join(out)


def load_json(body):
    if not body:
        return {}
    if isinstance(body, bytes):
        body = body.decode('utf-8')
    return json.loads(body)


def read_file_text(path):
    with open(path, 'r') as handle:
        return handle.read()


def read_file_bytes(path):
        with open(path, 'rb') as handle:
                return handle.read()


def file_size(path):
    return os.stat(path)[6]


def write_file_text(path, content):
    with open(path, 'w') as handle:
        handle.write(content)


def list_scripts():
    ensure_scripts_dir()
    files = []
    for name in os.listdir(SCRIPTS_DIR):
        full_path = SCRIPTS_DIR + '/' + name
        try:
            mode = os.stat(full_path)[0]
        except OSError:
            continue
        if mode & 0x4000:
            continue
        files.append(name)
    files.sort()
    return files


def file_path_from_request(path):
    name = url_decode(path[len('/api/files/'):])
    name = sanitize_name(name)
    ensure_scripts_dir()
    return name, SCRIPTS_DIR + '/' + name


def capture_exception(exc, buffer):
    if hasattr(sys, 'print_exception'):
        sys.print_exception(exc, buffer)
    else:
        buffer.write(repr(exc))


def execute_code(code, mode='auto'):
    global EXEC_DEPTH

    EXEC_DEPTH += 1
    buffer = OutputBuffer()
    had_print = 'print' in EXEC_GLOBALS
    previous_print = EXEC_GLOBALS.get('print')

    def captured_print(*args, **kwargs):
        separator = kwargs.get('sep', ' ')
        ending = kwargs.get('end', '\n')
        text = separator.join(str(arg) for arg in args)
        buffer.write(text + ending)

    try:
        EXEC_GLOBALS['print'] = captured_print

        result = None
        if mode == 'eval':
            result = eval(code, EXEC_GLOBALS)
        elif mode == 'exec':
            exec(code, EXEC_GLOBALS)
        else:
            try:
                result = eval(code, EXEC_GLOBALS)
            except SyntaxError:
                exec(code, EXEC_GLOBALS)

        output = buffer.getvalue()
        if result is not None:
            output += repr(result) + '\n'
        return {'ok': True, 'output': output.rstrip()}
    except Exception as exc:
        capture_exception(exc, buffer)
        return {'ok': False, 'error': buffer.getvalue().rstrip()}
    finally:
        if had_print:
            EXEC_GLOBALS['print'] = previous_print
        else:
            try:
                del EXEC_GLOBALS['print']
            except Exception:
                pass

        EXEC_DEPTH -= 1
        gc.collect()


def run_script(name):
    script_name = sanitize_name(name)
    path = SCRIPTS_DIR + '/' + script_name
    source = read_file_text(path)
    return execute_code(source, 'exec')


def current_ip():
    if network is None:
        return 'unknown'

    try:
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            return wlan.ifconfig()[0]
    except Exception:
        return 'unknown'
    return 'offline'


def status_payload():
    return {
        'ip': current_ip(),
        'heap_free': gc.mem_free(),
        'uptime_s': time.ticks_diff(time.ticks_ms(), START_TICKS) // 1000,
        'busy': EXEC_DEPTH > 0,
    }


def send_response(client, status='200 OK', body='', content_type='text/plain; charset=utf-8', headers=None):
    if isinstance(body, str):
        body = body.encode('utf-8')

    extra = headers or {}
    client.send(b'HTTP/1.1 ' + status.encode('utf-8') + b'\r\n')
    client.send(b'Connection: close\r\n')
    client.send(b'Content-Length: ' + str(len(body)).encode('utf-8') + b'\r\n')
    client.send(b'Content-Type: ' + content_type.encode('utf-8') + b'\r\n')
    for key, value in extra.items():
        line = key + ': ' + value + '\r\n'
        client.send(line.encode('utf-8'))
    client.send(b'\r\n')
    if body:
        client.send(body)


def send_json(client, payload, status='200 OK'):
    send_response(client, status, json.dumps(payload), 'application/json; charset=utf-8')


def send_file(client, path, content_type='application/octet-stream'):
    size = file_size(path)
    client.send(b'HTTP/1.1 200 OK\r\n')
    client.send(b'Connection: close\r\n')
    client.send(b'Content-Length: ' + str(size).encode('utf-8') + b'\r\n')
    client.send(b'Content-Type: ' + content_type.encode('utf-8') + b'\r\n')
    client.send(b'\r\n')

    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(1024)
            if not chunk:
                break
            client.send(chunk)


def static_path(name):
    return WEB_ROOT + '/' + name


def clear_boot_failures():
    try:
        os.remove(BOOT_FAILURE_FILE)
    except OSError:
        pass


def read_request(client):
    stream = client.makefile('rwb', 0)
    request_line = stream.readline()
    if not request_line:
        return None

    parts = request_line.decode('utf-8').strip().split()
    if len(parts) != 3:
        raise ValueError('Malformed request line')

    method, path, _ = parts
    headers = {}
    while True:
        line = stream.readline()
        if not line or line == b'\r\n':
            break
        text = line.decode('utf-8').strip()
        if ':' not in text:
            continue
        key, value = text.split(':', 1)
        headers[key.strip().lower()] = value.strip()

    length = int(headers.get('content-length', '0'))
    body = stream.read(length) if length else b''
    return method, path, headers, body


def handle_api(client, method, path, headers, body):
    if not is_authorized(headers):
        send_json(client, {'error': 'Unauthorized'}, '401 Unauthorized')
        return

    if method == 'GET' and path == '/api/status':
        send_json(client, status_payload())
        return

    if method == 'GET' and path == '/api/files':
        send_json(client, {'files': list_scripts()})
        return

    if path.startswith('/api/files/'):
        try:
            name, file_path = file_path_from_request(path)
        except ValueError as exc:
            send_json(client, {'error': str(exc)}, '400 Bad Request')
            return

        if method == 'GET':
            try:
                send_json(client, {'name': name, 'content': read_file_text(file_path)})
            except OSError:
                send_json(client, {'error': 'File not found'}, '404 Not Found')
            return

        if method == 'PUT':
            payload = load_json(body)
            write_file_text(file_path, payload.get('content', ''))
            send_json(client, {'ok': True, 'name': name})
            return

        if method == 'DELETE':
            try:
                os.remove(file_path)
            except OSError:
                send_json(client, {'error': 'File not found'}, '404 Not Found')
                return
            send_json(client, {'ok': True, 'name': name})
            return

    if method == 'POST' and path == '/api/exec':
        payload = load_json(body)
        result = execute_code(payload.get('code', ''), payload.get('mode', 'auto'))
        status = '200 OK' if result.get('ok') else '400 Bad Request'
        send_json(client, result, status)
        return

    if method == 'POST' and path == '/api/run':
        payload = load_json(body)
        try:
            result = run_script(payload.get('name', ''))
        except (OSError, ValueError) as exc:
            send_json(client, {'ok': False, 'error': str(exc)}, '400 Bad Request')
            return
        status = '200 OK' if result.get('ok') else '400 Bad Request'
        send_json(client, result, status)
        return

    if method == 'POST' and path == '/api/reset':
        send_json(client, {'ok': True, 'message': 'Reset requested'})
        if machine is not None:
            time.sleep(0.2)
            machine.reset()
        return

    send_json(client, {'error': 'Not found'}, '404 Not Found')


def handle_client(client):
    try:
        request = read_request(client)
        if request is None:
            return

        method, path, headers, body = request
        route = path.split('?', 1)[0]

        if route.startswith('/api/'):
            handle_api(client, method, route, headers, body)
            return

        if method == 'GET' and route == '/':
            send_file(client, static_path('index.html'), 'text/html; charset=utf-8')
            return

        if method == 'GET' and route == '/bg.jpg':
            try:
                send_file(client, 'bg.jpg', 'image/jpeg')
            except OSError:
                send_response(client, '404 Not Found', 'bg.jpg not found')
            return

        send_response(client, '404 Not Found', 'Not found')
    except Exception as exc:
        error_text = 'Request failed\n' + repr(exc)
        if hasattr(sys, 'print_exception'):
            sys.print_exception(exc)
        send_response(client, '500 Internal Server Error', error_text)
    finally:
        try:
            client.close()
        except Exception:
            pass


def start_server():
    ensure_scripts_dir()
    try:
        os.mkdir(WEB_ROOT)
    except OSError:
        pass

    addr = socket.getaddrinfo(HOST, PORT)[0][-1]
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(addr)
    server.listen(2)
    status_led = init_status_led()
    set_status_led(status_led, 0, 0, 32)
    clear_boot_failures()
    print('MicroShell listening on http://%s:%s' % (current_ip(), PORT))

    try:
        while True:
            client, _ = server.accept()
            handle_client(client)
    finally:
        set_status_led(status_led, 0, 0, 0)
        try:
            server.close()
        except Exception:
            pass


try:
    start_server()
except KeyboardInterrupt:
    print('MicroShell stopped.')
except Exception as exc:
    print('MicroShell startup failed:', exc)
    if hasattr(sys, 'print_exception'):
        sys.print_exception(exc)