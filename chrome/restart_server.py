#!/usr/bin/env python3
import http.server
import json
import os
import shutil
import signal
import threading
import time

DEFAULT_CONTROL_PORT = 9223
DEFAULT_PID_FILE_PATH = '/tmp/chrome-main.pid'
DEFAULT_RESTART_COOLDOWN_MS = 5000
DEFAULT_PROFILE_DIR = '/home/chromeuser/profile'

# Files and directories to clear for a fresh session (relative to profile dir)
# These contain session/cookie/tracking data that eBay uses to identify burned sessions
SESSION_PATHS = [
    # Main Default profile session data
    'Default/Cookies',
    'Default/Cookies-journal',
    'Default/History',
    'Default/History-journal',
    'Default/Local Storage',
    'Default/Session Storage',
    'Default/IndexedDB',
    'Default/Cache',
    'Default/Code Cache',
    'Default/Service Worker',
    'Default/blob_storage',
    'Default/DIPS',
    'Default/DIPS-journal',
    'Default/DIPS-wal',
    # First party sets can track sessions across domains
    'first_party_sets.db',
    'first_party_sets.db-journal',
]

_control_lock = threading.Lock()
_last_restart_epoch_ms = 0


def _get_logger_prefix():
    return '[STATUS]'


def _get_warn_prefix():
    return '[WARN]'


def _emit_status(message, **details):
    payload = {'message': message}
    if details:
        payload.update(details)
    print(f"{_get_logger_prefix()} Chrome control server: {json.dumps(payload, separators=(',', ':'))}", flush=True)


def _emit_warn(message, **details):
    payload = {'message': message}
    if details:
        payload.update(details)
    print(f"{_get_warn_prefix()} Chrome control server: {json.dumps(payload, separators=(',', ':'))}", flush=True)


def _clear_session_data(profile_dir):
    """
    Clear session/cookie data from Chrome profile to get a fresh fingerprint.

    This removes cookies, history, local storage, and other tracking data
    that sites like eBay use to identify burned sessions.

    Returns:
        dict: Summary of cleared paths and any errors
    """
    cleared = []
    errors = []
    skipped = []

    for rel_path in SESSION_PATHS:
        full_path = os.path.join(profile_dir, rel_path)

        if not os.path.exists(full_path):
            skipped.append(rel_path)
            continue

        try:
            if os.path.isdir(full_path):
                shutil.rmtree(full_path)
                cleared.append(rel_path)
            else:
                os.remove(full_path)
                cleared.append(rel_path)
        except OSError as err:
            errors.append({'path': rel_path, 'error': str(err)})

    return {
        'cleared': cleared,
        'skipped': skipped,
        'errors': errors
    }


class RestartRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = 'ThermopticChromeControl/1.0'

    def _read_pid(self):
        pid_file = self.server.pid_file
        try:
            with open(pid_file, 'r', encoding='utf-8') as handle:
                raw_pid = handle.read().strip()
            if not raw_pid:
                raise ValueError('PID file empty')
            return int(raw_pid)
        except (OSError, ValueError) as err:
            _emit_warn('Unable to load Chrome PID file.', error=str(err), pid_file=pid_file)
            return None

    def _kill_chrome(self):
        pid = self._read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            _emit_status('Sent SIGTERM to Chrome.', pid=pid)
            return True
        except ProcessLookupError:
            _emit_warn('Chrome PID not found during restart request.', pid=pid)
            return False
        except PermissionError as err:
            _emit_warn('Insufficient permissions to signal Chrome.', error=str(err), pid=pid)
            return False

    def _handle_restart(self, clear_profile=False):
        global _last_restart_epoch_ms
        now_ms = int(time.time() * 1000)
        cooldown_ms = self.server.restart_cooldown_ms

        with _control_lock:
            if now_ms - _last_restart_epoch_ms < cooldown_ms:
                self.send_response(429)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                payload = {'status': 'cooldown', 'cooldown_ms': cooldown_ms}
                self.wfile.write(json.dumps(payload).encode('utf-8'))
                return

            restart_triggered = self._kill_chrome()
            clear_result = None

            if restart_triggered and clear_profile:
                # Wait a moment for Chrome to fully stop before clearing files
                time.sleep(0.5)
                profile_dir = self.server.profile_dir
                clear_result = _clear_session_data(profile_dir)
                _emit_status(
                    'Cleared session data for fresh fingerprint.',
                    cleared_count=len(clear_result['cleared']),
                    error_count=len(clear_result['errors'])
                )

            if restart_triggered:
                _last_restart_epoch_ms = now_ms

        status_code = 202 if restart_triggered else 500
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        payload = {'status': 'restarting' if restart_triggered else 'failed'}
        if clear_result:
            payload['profile_cleared'] = True
            payload['cleared_paths'] = clear_result['cleared']
            if clear_result['errors']:
                payload['clear_errors'] = clear_result['errors']
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def _handle_clear_profile(self):
        """Clear session data without restarting Chrome (useful for pre-clearing)."""
        profile_dir = self.server.profile_dir
        clear_result = _clear_session_data(profile_dir)

        _emit_status(
            'Cleared session data.',
            cleared_count=len(clear_result['cleared']),
            error_count=len(clear_result['errors'])
        )

        status_code = 200 if not clear_result['errors'] else 207  # 207 = Multi-Status
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        payload = {
            'status': 'cleared',
            'cleared_paths': clear_result['cleared'],
            'skipped_paths': clear_result['skipped']
        }
        if clear_result['errors']:
            payload['errors'] = clear_result['errors']
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def do_POST(self):
        if self.path == '/restart':
            self._handle_restart(clear_profile=False)
        elif self.path == '/fresh-restart':
            # Restart Chrome and clear session data for a fresh fingerprint
            self._handle_restart(clear_profile=True)
        elif self.path == '/clear-profile':
            # Clear session data without restarting (Chrome should be stopped first)
            self._handle_clear_profile()
        else:
            self.send_error(404, 'Not Found')

    def do_GET(self):
        if self.path == '/status':
            pid = self._read_pid()
            state = 'ready' if pid is not None else 'unknown'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            payload = {'status': state, 'pid': pid}
            self.wfile.write(json.dumps(payload).encode('utf-8'))
        else:
            self.send_error(404, 'Not Found')

    def log_message(self, format, *args):
        return


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, pid_file, restart_cooldown_ms, profile_dir):
        super().__init__(server_address, RequestHandlerClass)
        self.pid_file = pid_file
        self.restart_cooldown_ms = restart_cooldown_ms
        self.profile_dir = profile_dir


def main():
    port = int(os.environ.get('CHROME_CONTROL_PORT', DEFAULT_CONTROL_PORT))
    pid_file = os.environ.get('CHROME_CONTROL_PID_FILE', DEFAULT_PID_FILE_PATH)
    restart_cooldown_ms = int(os.environ.get('CHROME_CONTROL_COOLDOWN_MS', DEFAULT_RESTART_COOLDOWN_MS))
    profile_dir = os.environ.get('CHROME_PROFILE_DIR', DEFAULT_PROFILE_DIR)

    server = ThreadedHTTPServer(
        ('0.0.0.0', port), RestartRequestHandler, pid_file, restart_cooldown_ms, profile_dir
    )
    _emit_status('Chrome restart control server listening.', port=port, pid_file=pid_file, profile_dir=profile_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _emit_status('Chrome restart control server stopped.')


if __name__ == '__main__':
    main()
