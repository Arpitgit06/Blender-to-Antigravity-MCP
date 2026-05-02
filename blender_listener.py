"""
╔══════════════════════════════════════════════════════════════════╗
║         BLENDER MCP BRIDGE — LISTENER (v2.0)                   ║
║  Run this inside Blender > Scripting tab > Run Script           ║
║  DO NOT close the console after running — it keeps the server   ║
╚══════════════════════════════════════════════════════════════════╝

CRITICAL ARCHITECTURE NOTE:
  Blender's `bpy` API is NOT thread-safe. All bpy calls MUST run on
  Blender's main thread. This listener uses a command queue + a
  bpy.app.timers callback to safely bridge socket I/O (background
  thread) with bpy execution (main thread).
"""

import bpy
import socket
import threading
import queue
import json
import traceback
import time
import sys
import os
import logging
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────────────────
HOST          = "127.0.0.1"
PORT          = 9876
BUFFER_SIZE   = 65536          # 64 KB per recv — large enough for complex code
TIMER_INTERVAL = 0.05          # 50ms polling — fast enough, low CPU overhead
MAX_QUEUE_SIZE = 100           # Prevent runaway queues
RECV_TIMEOUT  = 30.0           # Socket read timeout (seconds)
SERVER_VERSION = "2.0.0"

# ─── Logging Setup ───────────────────────────────────────────────────────────
log_path = os.path.join(os.path.expanduser("~"), "blender_mcp_listener.log")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("BlenderMCP")

# ─── Thread-safe queues ───────────────────────────────────────────────────────
# Each item: {"code": str, "response_queue": Queue, "request_id": str}
_command_queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)

# Tracks whether the server thread + timer are already running
_server_running = False
_server_thread  = None
_server_socket  = None


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1: MAIN-THREAD COMMAND EXECUTOR
#  Registered as a bpy.app.timers callback — runs on Blender's main thread
# ══════════════════════════════════════════════════════════════════════════════

def _execute_pending_commands() -> float:
    """
    Drains the command queue and runs each code string via exec().
    Runs on the main thread via bpy.app.timers — this is the ONLY safe
    way to call bpy functions from a background thread.
    Returns the interval (seconds) until the next call.
    """
    while not _command_queue.empty():
        try:
            item = _command_queue.get_nowait()
        except queue.Empty:
            break

        code        = item.get("code", "")
        response_q  = item.get("response_queue")
        request_id  = item.get("request_id", "unknown")

        log.debug(f"[{request_id}] Executing code block ({len(code)} chars)")

        try:
            # Build a safe exec namespace pre-loaded with common modules
            exec_globals = {
                "bpy":      bpy,
                "mathutils": __import__("mathutils"),
                "os":       os,
                "json":     json,
                "time":     time,
                "_result":  None,       # Scripts can set _result to return data
                "_output":  [],         # Scripts can append to _output for multi-line results
            }
            exec(compile(code, f"<mcp_request_{request_id}>", "exec"), exec_globals)

            # Collect result: prefer _result, then _output list, then generic OK
            result = exec_globals.get("_result")
            output = exec_globals.get("_output", [])

            if result is not None:
                payload = str(result)
            elif output:
                payload = "\n".join(str(x) for x in output)
            else:
                payload = "OK"

            response = {
                "status":     "success",
                "result":     payload,
                "request_id": request_id,
                "blender_version": ".".join(str(v) for v in bpy.app.version),
                "timestamp":  datetime.utcnow().isoformat(),
            }
        except Exception:
            tb = traceback.format_exc()
            log.error(f"[{request_id}] Execution error:\n{tb}")
            response = {
                "status":     "error",
                "error":      tb,
                "request_id": request_id,
                "timestamp":  datetime.utcnow().isoformat(),
            }

        # Put response back so the socket handler thread can read it
        if response_q is not None:
            response_q.put(response)

    return TIMER_INTERVAL


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2: SOCKET SERVER (runs in background thread)
# ══════════════════════════════════════════════════════════════════════════════

def _handle_client(conn: socket.socket, addr: tuple) -> None:
    """Handles a single client connection in its own thread."""
    log.info(f"Client connected: {addr}")
    conn.settimeout(RECV_TIMEOUT)
    try:
        # ── 1. Read the full message (length-prefixed framing) ────────────
        # Protocol: 4-byte big-endian uint32 length header + JSON body
        raw_header = _recv_exactly(conn, 4)
        if raw_header is None:
            log.warning(f"[{addr}] Empty header — closing")
            return

        msg_len = int.from_bytes(raw_header, "big")
        if msg_len > 10 * 1024 * 1024:     # 10 MB hard cap
            log.error(f"[{addr}] Message too large ({msg_len} bytes) — rejecting")
            _send_response(conn, {"status": "error", "error": "Message exceeds 10MB limit"})
            return

        raw_body = _recv_exactly(conn, msg_len)
        if raw_body is None:
            log.warning(f"[{addr}] Incomplete message — closing")
            return

        # ── 2. Parse JSON payload ─────────────────────────────────────────
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as e:
            _send_response(conn, {"status": "error", "error": f"Invalid JSON: {e}"})
            return

        action     = payload.get("action", "exec")
        code       = payload.get("code", "")
        request_id = payload.get("request_id", f"req_{int(time.time()*1000)}")

        log.info(f"[{request_id}] Action={action} from {addr}")

        # ── 3. Handle built-in actions (no bpy needed) ────────────────────
        if action == "ping":
            _send_response(conn, {
                "status":         "pong",
                "server_version": SERVER_VERSION,
                "blender_version": ".".join(str(v) for v in bpy.app.version),
                "request_id":     request_id,
            })
            return

        if action == "shutdown":
            _send_response(conn, {"status": "ok", "message": "Listener shutting down"})
            stop_server()
            return

        # ── 4. Queue bpy code for main-thread execution ───────────────────
        response_q: queue.Queue = queue.Queue()
        try:
            _command_queue.put_nowait({
                "code":           code,
                "response_queue": response_q,
                "request_id":     request_id,
            })
        except queue.Full:
            _send_response(conn, {
                "status": "error",
                "error":  "Command queue full — Blender is busy. Try again shortly.",
            })
            return

        # ── 5. Wait for main thread to finish execution ───────────────────
        try:
            result = response_q.get(timeout=60.0)   # 60s max wait for exec
        except queue.Empty:
            _send_response(conn, {
                "status": "error",
                "error":  "Execution timed out after 60 seconds",
            })
            return

        _send_response(conn, result)

    except socket.timeout:
        log.warning(f"[{addr}] Socket timeout")
    except ConnectionResetError:
        log.warning(f"[{addr}] Connection reset by client")
    except Exception:
        log.error(f"[{addr}] Unexpected error:\n{traceback.format_exc()}")
        try:
            _send_response(conn, {"status": "error", "error": "Internal listener error"})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        log.info(f"Client disconnected: {addr}")


def _recv_exactly(conn: socket.socket, n: int):
    """Reads exactly n bytes from the socket. Returns None on failure."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = conn.recv(min(n - len(buf), BUFFER_SIZE))
        except socket.timeout:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _send_response(conn: socket.socket, data: dict) -> None:
    """Sends a length-prefixed JSON response."""
    body   = json.dumps(data, default=str).encode("utf-8")
    header = len(body).to_bytes(4, "big")
    conn.sendall(header + body)


def _server_loop(server_sock: socket.socket) -> None:
    """Main accept loop — runs in a daemon thread."""
    log.info(f"Blender MCP Listener ready on {HOST}:{PORT}")
    while True:
        try:
            conn, addr = server_sock.accept()
        except OSError:
            # Socket was closed (server stopped)
            break
        t = threading.Thread(
            target=_handle_client,
            args=(conn, addr),
            daemon=True,
            name=f"mcp-client-{addr[1]}"
        )
        t.start()
    log.info("Server loop exited.")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3: START / STOP API
# ══════════════════════════════════════════════════════════════════════════════

def start_server() -> None:
    global _server_running, _server_thread, _server_socket

    if _server_running:
        log.warning("Server is already running — skipping start.")
        return

    # Create TCP socket with SO_REUSEADDR so we can restart without waiting
    _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _server_socket.bind((HOST, PORT))
    except OSError as e:
        log.error(f"Cannot bind to {HOST}:{PORT}: {e}")
        log.error("Is another Blender instance already running the listener?")
        return

    _server_socket.listen(10)

    # Start background accept thread
    _server_thread = threading.Thread(
        target=_server_loop,
        args=(_server_socket,),
        daemon=True,
        name="blender-mcp-acceptor",
    )
    _server_thread.start()

    # Register the main-thread timer for bpy execution
    if not bpy.app.timers.is_registered(_execute_pending_commands):
        bpy.app.timers.register(_execute_pending_commands, persistent=True)
        log.info(f"Timer registered (interval={TIMER_INTERVAL}s)")

    _server_running = True
    log.info("═" * 60)
    log.info(f"  Blender MCP Listener v{SERVER_VERSION} STARTED")
    log.info(f"  Listening on {HOST}:{PORT}")
    log.info(f"  Log file: {log_path}")
    log.info("═" * 60)


def stop_server() -> None:
    global _server_running, _server_socket

    if not _server_running:
        return

    # Unregister timer
    if bpy.app.timers.is_registered(_execute_pending_commands):
        bpy.app.timers.unregister(_execute_pending_commands)

    # Close server socket (this unblocks server_sock.accept())
    if _server_socket:
        try:
            _server_socket.close()
        except Exception:
            pass
        _server_socket = None

    _server_running = False
    log.info("Blender MCP Listener stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    start_server()
    # ── Quick self-test ───────────────────────────────────────────────────────
    print("\n✅  Listener started. You should see this in the Blender console.")
    print(f"    Bound to: {HOST}:{PORT}")
    print(f"    Log:      {log_path}")
    print("\n    To verify: run test_connection.py from your terminal.")
    print("    To stop:   call stop_server() in the scripting tab.\n")
