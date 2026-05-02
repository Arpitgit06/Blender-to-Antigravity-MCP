"""
╔══════════════════════════════════════════════════════════════════╗
║         BLENDER MCP BRIDGE — PIPELINE TEST SUITE               ║
║  Run from terminal: python test_connection.py                   ║
║  Blender must be open with blender_listener.py running first.  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import socket
import struct
import sys
import textwrap
import time
import uuid

HOST = "127.0.0.1"
PORT = 9876

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⏭  SKIP"

results = []


# ─── Transport helpers ───────────────────────────────────────────────────────

def send_command(payload: dict, timeout: float = 30.0) -> dict:
    body   = json.dumps(payload, default=str).encode("utf-8")
    header = struct.pack(">I", len(body))
    try:
        with socket.create_connection((HOST, PORT), timeout=5.0) as sock:
            sock.settimeout(timeout)
            sock.sendall(header + body)
            # Read response
            raw_len = _recv_exactly(sock, 4)
            if not raw_len:
                return {"status": "error", "error": "No response header"}
            resp_len = struct.unpack(">I", raw_len)[0]
            raw_body = _recv_exactly(sock, resp_len)
            if not raw_body:
                return {"status": "error", "error": "Incomplete body"}
            return json.loads(raw_body.decode("utf-8"))
    except ConnectionRefusedError:
        return {"status": "error", "error": f"Connection refused — is Blender running with the listener?"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _recv_exactly(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def run_test(name: str, payload: dict, expect_status: str = "success") -> bool:
    print(f"\n{'─'*60}")
    print(f"  TEST: {name}")
    print(f"{'─'*60}")

    t0 = time.monotonic()
    result = send_command(payload)
    elapsed = time.monotonic() - t0

    status = result.get("status", "")
    passed = (status == expect_status) or \
             (expect_status == "success" and status == "pong")

    icon = PASS if passed else FAIL
    print(f"  Status  : {status}")
    if status == "success":
        print(f"  Result  : {result.get('result', '')[:200]}")
    elif status == "pong":
        print(f"  Blender : {result.get('blender_version')}")
        print(f"  Server  : {result.get('server_version')}")
    else:
        print(f"  Error   : {result.get('error', '')[:300]}")
    print(f"  Time    : {elapsed:.2f}s")
    print(f"  {icon}")

    results.append((name, passed, elapsed))
    return passed


# ══════════════════════════════════════════════════════════════════════════════
#  TEST CASES
# ══════════════════════════════════════════════════════════════════════════════

def test_ping():
    return run_test("Ping (connection check)", {"action": "ping"}, "pong")


def test_hello_world():
    code = textwrap.dedent("""
        # Clear scene
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)

        # Hello Cube
        bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
        obj = bpy.context.active_object
        obj.name = "TEST_HelloCube"

        # Green material
        mat = bpy.data.materials.new("TEST_GreenMetal")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Base Color"].default_value  = (0.0, 1.0, 0.3, 1.0)
        bsdf.inputs["Metallic"].default_value    = 0.8
        bsdf.inputs["Roughness"].default_value   = 0.15
        obj.data.materials.append(mat)

        _result = f"HelloCube created: {obj.name} | Material: {mat.name}"
    """).strip()
    return run_test("Hello World — green cube", {"action": "exec", "code": code})


def test_scene_info():
    code = textwrap.dedent("""
        import json as _json
        data = {
            "scene": bpy.context.scene.name,
            "objects": [{"name": o.name, "type": o.type} for o in bpy.context.scene.objects],
        }
        _result = _json.dumps(data, indent=2)
    """).strip()
    return run_test("Scene info query", {"action": "exec", "code": code})


def test_transform():
    code = textwrap.dedent("""
        import math
        obj = bpy.data.objects.get("TEST_HelloCube")
        if obj is None:
            raise ValueError("TEST_HelloCube not found — run Hello World test first")
        obj.location    = (2.0, 1.0, 0.5)
        obj.rotation_euler.z = math.radians(45)
        obj.scale       = (1.5, 1.5, 1.5)
        _result = f"Moved to {list(obj.location)}, rotated 45°, scaled 1.5x"
    """).strip()
    return run_test("Transform object (move/rotate/scale)", {"action": "exec", "code": code})


def test_error_handling():
    code = "this is not valid python !!!"
    result = send_command({"action": "exec", "code": code})
    passed = result.get("status") == "error"
    name = "Error handling (invalid code → clean error)"
    icon = PASS if passed else FAIL
    print(f"\n{'─'*60}")
    print(f"  TEST: {name}")
    print(f"{'─'*60}")
    print(f"  Status  : {result.get('status')}")
    print(f"  Got error message: {'error' in result}")
    print(f"  {icon}")
    results.append((name, passed, 0))
    return passed


def test_multiline_result():
    code = textwrap.dedent("""
        for i in range(3):
            _output.append(f"Object {i}: created at ({i}, {i}, 0)")
    """).strip()
    return run_test("Multi-line _output result", {"action": "exec", "code": code})


def test_math_import():
    code = textwrap.dedent("""
        import math
        # Verify math module available + bpy works together
        val = math.sin(math.radians(90))
        _result = f"sin(90°) = {val:.4f}  | bpy.app.version = {bpy.app.version}"
    """).strip()
    return run_test("Python stdlib + bpy interop", {"action": "exec", "code": code})


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║        BLENDER MCP BRIDGE — TEST SUITE                     ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"\nTarget: {HOST}:{PORT}\n")

    # Run tests in order — skip remaining if ping fails
    if not test_ping():
        print("\n⚠️  PING FAILED — aborting remaining tests.")
        print("    → Open Blender, go to Scripting tab, paste blender_listener.py, click Run.")
        sys.exit(1)

    test_hello_world()
    test_scene_info()
    test_transform()
    test_error_handling()
    test_multiline_result()
    test_math_import()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  SUMMARY")
    print(f"{'═'*60}")
    passed_count = sum(1 for _, p, _ in results if p)
    total = len(results)
    for name, passed, elapsed in results:
        icon = "✅" if passed else "❌"
        print(f"  {icon}  {name:<45} ({elapsed:.2f}s)")
    print(f"{'─'*60}")
    print(f"  Passed: {passed_count}/{total}")
    if passed_count == total:
        print("\n  🎉  All tests passed! Your pipeline is ready.")
        print("     You can now use the MCP server from AntiGravity IDE.")
    else:
        print(f"\n  ⚠️  {total - passed_count} test(s) failed. Check the log above.")
    print()
    sys.exit(0 if passed_count == total else 1)
