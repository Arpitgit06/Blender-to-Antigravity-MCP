"""
╔══════════════════════════════════════════════════════════════════╗
║       BLENDER MCP BRIDGE — MCP SERVER (v2.0)                   ║
║  Standalone FastMCP server — registered in AntiGravity IDE      ║
║  Run: python mcp_server.py                                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import struct
import sys
import textwrap
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

# ─── FastMCP import with helpful error ───────────────────────────────────────
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: 'mcp' package not found. Run: pip install mcp fastmcp")
    sys.exit(1)

# ─── Configuration (override via environment variables) ──────────────────────
BLENDER_HOST    = os.getenv("BLENDER_MCP_HOST", "127.0.0.1")
BLENDER_PORT    = int(os.getenv("BLENDER_MCP_PORT", "9876"))
CONNECT_TIMEOUT = float(os.getenv("BLENDER_CONNECT_TIMEOUT", "5.0"))
EXEC_TIMEOUT    = float(os.getenv("BLENDER_EXEC_TIMEOUT", "60.0"))
SERVER_VERSION  = "2.0.0"

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("BlenderMCP.Server")

# ─── FastMCP app ─────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="blender-bridge",
    version=SERVER_VERSION,
    description=(
        "MCP server that connects AntiGravity IDE to a live Blender instance. "
        "Allows natural language 3D modelling, scene inspection, and GLB export "
        "directly into your web project's /public folder."
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1: LOW-LEVEL BLENDER SOCKET TRANSPORT
# ══════════════════════════════════════════════════════════════════════════════

class BlenderConnectionError(RuntimeError):
    """Raised when we cannot reach the Blender listener."""


def _send_to_blender(payload: dict, timeout: float = EXEC_TIMEOUT) -> dict:
    """
    Sends a JSON payload to the Blender listener using the length-prefixed
    protocol and returns the parsed response dict.

    Protocol  (must match blender_listener.py):
      [4-byte big-endian uint32 = body length] [UTF-8 JSON body]
    """
    request_id = payload.setdefault("request_id", str(uuid.uuid4())[:8])
    body   = json.dumps(payload, default=str).encode("utf-8")
    header = struct.pack(">I", len(body))

    try:
        with socket.create_connection((BLENDER_HOST, BLENDER_PORT),
                                      timeout=CONNECT_TIMEOUT) as sock:
            sock.settimeout(timeout)
            sock.sendall(header + body)

            # Read response header (4 bytes)
            raw_len = _recv_exactly(sock, 4, timeout)
            if raw_len is None:
                raise BlenderConnectionError("No response header received from Blender.")
            resp_len = struct.unpack(">I", raw_len)[0]

            # Read response body
            raw_body = _recv_exactly(sock, resp_len, timeout)
            if raw_body is None:
                raise BlenderConnectionError("Incomplete response body from Blender.")

            response = json.loads(raw_body.decode("utf-8"))
            log.debug(f"[{request_id}] Response status={response.get('status')}")
            return response

    except (ConnectionRefusedError, TimeoutError, socket.timeout) as e:
        raise BlenderConnectionError(
            f"Cannot reach Blender listener at {BLENDER_HOST}:{BLENDER_PORT}.\n"
            f"  → Make sure Blender is open and blender_listener.py has been run.\n"
            f"  → Original error: {type(e).__name__}: {e}"
        )
    except json.JSONDecodeError as e:
        raise BlenderConnectionError(f"Malformed JSON response from Blender: {e}")


def _recv_exactly(sock: socket.socket, n: int, timeout: float) -> bytes | None:
    """Reads exactly n bytes, respecting the overall timeout."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(min(remaining, 5.0))
        try:
            chunk = sock.recv(min(n - len(buf), 65536))
        except socket.timeout:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _exec_bpy(code: str, description: str = "") -> str:
    """
    Sends raw bpy Python code to Blender and returns a formatted result string.
    Raises on connection errors; returns error details on execution errors.
    """
    log.info(f"Sending to Blender: {description or 'code block'} ({len(code)} chars)")
    try:
        result = _send_to_blender({"action": "exec", "code": code})
    except BlenderConnectionError as e:
        return f"❌  BLENDER OFFLINE\n{e}"

    if result.get("status") == "success":
        payload = result.get("result", "OK")
        blender_ver = result.get("blender_version", "unknown")
        return f"✅  Success (Blender {blender_ver})\n{payload}"
    else:
        error = result.get("error", "Unknown error")
        return f"❌  Blender execution error:\n{error}"


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2: BPY CODE GENERATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

_NL_PATTERNS = [
    # Primitives
    (r"(?:add|create|make|insert)\s+(?:a\s+)?(?:new\s+)?cube",
     "bpy.ops.mesh.primitive_cube_add(size=2, location=(0,0,0))"),
    (r"(?:add|create|make|insert)\s+(?:a\s+)?(?:new\s+)?sphere",
     "bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=(0,0,0))"),
    (r"(?:add|create|make|insert)\s+(?:a\s+)?(?:new\s+)?cylinder",
     "bpy.ops.mesh.primitive_cylinder_add(radius=1, depth=2, location=(0,0,0))"),
    (r"(?:add|create|make|insert)\s+(?:a\s+)?(?:new\s+)?plane",
     "bpy.ops.mesh.primitive_plane_add(size=2, location=(0,0,0))"),
    (r"(?:add|create|make|insert)\s+(?:a\s+)?(?:new\s+)?torus",
     "bpy.ops.mesh.primitive_torus_add(location=(0,0,0))"),
    (r"(?:add|create|make|insert)\s+(?:a\s+)?(?:new\s+)?cone",
     "bpy.ops.mesh.primitive_cone_add(location=(0,0,0))"),
    # Deletion
    (r"(?:delete|remove|clear)\s+(?:all\s+)?(?:objects|everything|scene)",
     "bpy.ops.object.select_all(action='SELECT')\nbpy.ops.object.delete(use_global=False)"),
    (r"(?:delete|remove)\s+(?:the\s+)?(?:selected|active)\s+object",
     "bpy.ops.object.delete(use_global=False)"),
    # Viewport / rendering
    (r"(?:set|switch|change)\s+(?:to\s+)?(?:rendered|render)\s+(?:view|shading)",
     "bpy.context.space_data.shading.type = 'RENDERED'"),
    (r"(?:set|switch|change)\s+(?:to\s+)?(?:solid|solid)\s+(?:view|shading)",
     "bpy.context.space_data.shading.type = 'SOLID'"),
]


def _natural_language_to_bpy(request: str) -> str | None:
    """
    Attempts a fast regex-based NL → bpy translation for common operations.
    Returns None if no pattern matched (caller should use the AI fallback).
    """
    lower = request.lower().strip()
    for pattern, code in _NL_PATTERNS:
        if re.search(pattern, lower):
            return code
    return None


def _build_scene_query_code() -> str:
    return textwrap.dedent("""
        import json as _json
        data = {
            "objects": [],
            "materials": [],
            "scene_name": bpy.context.scene.name,
            "frame_current": bpy.context.scene.frame_current,
            "render_engine": bpy.context.scene.render.engine,
        }
        for obj in bpy.context.scene.objects:
            entry = {
                "name":     obj.name,
                "type":     obj.type,
                "location": list(obj.location),
                "rotation": list(obj.rotation_euler),
                "scale":    list(obj.scale),
                "visible":  obj.visible_get(),
                "selected": obj.select_get(),
            }
            if obj.data and hasattr(obj.data, 'polygons'):
                entry["mesh_polys"] = len(obj.data.polygons)
            if obj.material_slots:
                entry["materials"] = [s.material.name for s in obj.material_slots if s.material]
            data["objects"].append(entry)
        for mat in bpy.data.materials:
            data["materials"].append({
                "name":       mat.name,
                "use_nodes":  mat.use_nodes,
                "users":      mat.users,
            })
        _result = _json.dumps(data, indent=2)
    """).strip()


def _build_export_code(export_path: str, fmt: str) -> str:
    safe_path = export_path.replace("\\", "/")
    if fmt == "glb":
        return textwrap.dedent(f"""
            import os as _os
            _os.makedirs(_os.path.dirname("{safe_path}"), exist_ok=True)
            bpy.ops.export_scene.gltf(
                filepath="{safe_path}",
                export_format='GLB',
                export_apply=True,
                export_animations=True,
                export_lights=True,
                export_cameras=True,
                export_extras=True,
            )
            _result = "Exported GLB to: {safe_path}"
        """).strip()
    elif fmt == "gltf":
        return textwrap.dedent(f"""
            import os as _os
            _os.makedirs(_os.path.dirname("{safe_path}"), exist_ok=True)
            bpy.ops.export_scene.gltf(
                filepath="{safe_path}",
                export_format='GLTF_SEPARATE',
                export_apply=True,
                export_animations=True,
            )
            _result = "Exported GLTF to: {safe_path}"
        """).strip()
    else:
        return f'raise ValueError("Unsupported format: {fmt}. Use glb or gltf.")'


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3: MCP TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def ping_blender() -> str:
    """
    Checks if the Blender listener is alive and returns version information.
    Always call this first to verify the connection before issuing commands.
    """
    try:
        result = _send_to_blender({"action": "ping"})
        if result.get("status") == "pong":
            return (
                f"✅  Blender listener is ONLINE\n"
                f"   Blender version : {result.get('blender_version', 'unknown')}\n"
                f"   Server version  : {result.get('server_version', 'unknown')}\n"
                f"   Port            : {BLENDER_HOST}:{BLENDER_PORT}"
            )
        return f"⚠️  Unexpected response: {result}"
    except BlenderConnectionError as e:
        return f"❌  Blender is OFFLINE:\n{e}"


@mcp.tool()
def run_blender_command(
    request: str,
    raw_code: str = "",
    context_hint: str = "",
) -> str:
    """
    Execute a 3D operation in Blender.

    Parameters
    ----------
    request     : Natural language description of what you want to do, e.g.
                  "add a red metallic sphere at position (2, 0, 1)".
                  Used for logging and NL→bpy fast-path matching.
    raw_code    : (Optional) Explicit bpy Python code to run verbatim.
                  When provided, `request` is used only for logging.
    context_hint: (Optional) Hint for code generation, e.g. "mesh editing",
                  "material", "animation", "lighting".

    Returns
    -------
    A string describing success or the error traceback from Blender.

    Tips
    ----
    - Always use `bpy.context.scene` not `bpy.context.window`
    - Deselect before selecting: bpy.ops.object.select_all(action='DESELECT')
    - Set active object: bpy.context.view_layer.objects.active = obj
    - Set _result = "your message" at the end to return a custom result
    """
    # 1. Use raw_code if explicitly provided
    if raw_code.strip():
        code = raw_code
        log.info(f"Using raw_code for: {request}")
    else:
        # 2. Fast NL → bpy regex matching
        code = _natural_language_to_bpy(request)
        if code:
            log.info(f"NL fast-path matched: {request[:60]}")
        else:
            # 3. Generic fallback — AI should prefer raw_code path
            log.info(f"No NL pattern for: {request[:60]} — using request as comment")
            code = f"# Request: {request}\n# Provide raw_code for complex operations."

    return _exec_bpy(code, description=request[:80])


@mcp.tool()
def get_scene_info() -> str:
    """
    Returns a full JSON snapshot of the current Blender scene:
    objects, materials, scene settings, render engine, etc.

    Use this to understand scene state before modifying it.
    The JSON structure:
      {
        "scene_name": str,
        "frame_current": int,
        "render_engine": str,
        "objects": [ { name, type, location, rotation, scale, visible,
                        selected, mesh_polys?, materials? }, ... ],
        "materials": [ { name, use_nodes, users }, ... ]
      }
    """
    return _exec_bpy(_build_scene_query_code(), description="get_scene_info")


@mcp.tool()
def select_object(name: str) -> str:
    """
    Selects and sets active a Blender object by name.

    Parameters
    ----------
    name : Exact Blender object name (case-sensitive), e.g. "Cube", "Sphere.001"
    """
    code = textwrap.dedent(f"""
        bpy.ops.object.select_all(action='DESELECT')
        obj = bpy.data.objects.get("{name}")
        if obj is None:
            raise ValueError("Object '{name}' not found in scene.")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        _result = f"Selected: {{obj.name}} (type={{obj.type}})"
    """).strip()
    return _exec_bpy(code, f"select_object({name})")


@mcp.tool()
def apply_material(
    object_name: str,
    material_name: str,
    base_color: list[float] | None = None,
    metallic: float = 0.0,
    roughness: float = 0.5,
    emission_color: list[float] | None = None,
    emission_strength: float = 0.0,
) -> str:
    """
    Creates (or updates) a PBR material and applies it to an object.

    Parameters
    ----------
    object_name      : Target object name (must exist in scene)
    material_name    : Name for the material (created if it doesn't exist)
    base_color       : RGBA float list [R, G, B, A], values 0.0–1.0
    metallic         : 0.0 (plastic) to 1.0 (metal)
    roughness        : 0.0 (mirror) to 1.0 (matte)
    emission_color   : RGBA for emission glow (optional)
    emission_strength: Emission intensity (default 0 = off)
    """
    bc   = base_color or [0.8, 0.8, 0.8, 1.0]
    emit = emission_color or [1.0, 1.0, 1.0, 1.0]

    code = textwrap.dedent(f"""
        obj = bpy.data.objects.get("{object_name}")
        if obj is None:
            raise ValueError("Object '{object_name}' not found.")

        mat = bpy.data.materials.get("{material_name}")
        if mat is None:
            mat = bpy.data.materials.new(name="{material_name}")
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        bsdf = nodes.get("Principled BSDF")
        if bsdf is None:
            nodes.clear()
            bsdf = nodes.new("ShaderNodeBsdfPrincipled")
            output = nodes.new("ShaderNodeOutputMaterial")
            mat.node_tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

        bsdf.inputs["Base Color"].default_value   = {bc}
        bsdf.inputs["Metallic"].default_value     = {metallic}
        bsdf.inputs["Roughness"].default_value    = {roughness}
        bsdf.inputs["Emission Color"].default_value = {emit}
        bsdf.inputs["Emission Strength"].default_value = {emission_strength}

        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)

        _result = f"Material '{{mat.name}}' applied to '{{obj.name}}'"
    """).strip()
    return _exec_bpy(code, f"apply_material({object_name}, {material_name})")


@mcp.tool()
def transform_object(
    name: str,
    location: list[float] | None = None,
    rotation_euler_deg: list[float] | None = None,
    scale: list[float] | None = None,
) -> str:
    """
    Moves, rotates, or scales an object.

    Parameters
    ----------
    name                 : Object name (e.g. "Cube")
    location             : [X, Y, Z] world position in Blender units
    rotation_euler_deg   : [X, Y, Z] rotation in DEGREES (converted internally)
    scale                : [X, Y, Z] scale factors (1.0 = original size)
    """
    loc  = location or None
    rot  = rotation_euler_deg or None
    scl  = scale or None

    parts = [f'obj = bpy.data.objects.get("{name}")',
             f'if obj is None: raise ValueError("Object \\"{name}\\" not found")']
    if loc:
        parts.append(f"obj.location = {loc}")
    if rot:
        parts.append("import math as _math")
        parts.append(f"obj.rotation_euler = [_math.radians(a) for a in {rot}]")
    if scl:
        parts.append(f"obj.scale = {scl}")
    parts.append('_result = f"Transformed: {obj.name} loc={list(obj.location)}"')

    code = "\n".join(parts)
    return _exec_bpy(code, f"transform_object({name})")


@mcp.tool()
def trigger_web_export(
    public_folder: str,
    filename: str = "scene",
    fmt: str = "glb",
    export_selection_only: bool = False,
) -> str:
    """
    Exports the current Blender scene as .glb or .gltf into your web project's
    /public folder so it can be loaded by Three.js / React Three Fiber / Babylon.js.

    Parameters
    ----------
    public_folder         : Absolute path to your project's /public directory.
                            Example: "/Users/you/myapp/public"
                            Example: "C:/Users/you/myapp/public"
    filename              : Output filename WITHOUT extension (default: "scene")
    fmt                   : "glb" (single binary file, recommended) or "gltf"
    export_selection_only : If True, only exports selected objects.

    Returns
    -------
    Success message with the full output path.
    """
    if not public_folder:
        return "❌  public_folder is required. Provide the absolute path to your /public directory."

    # Normalize path separators
    public_folder = public_folder.replace("\\", "/").rstrip("/")
    ext       = "glb" if fmt == "glb" else "gltf"
    out_path  = f"{public_folder}/{filename}.{ext}"

    code = _build_export_code(out_path, fmt)

    if export_selection_only:
        # Prepend export_selected flag note
        code = code.replace(
            "export_apply=True",
            "export_apply=True,\n    use_selection=True"
        )

    result = _exec_bpy(code, f"trigger_web_export → {out_path}")

    if "Success" in result:
        return (
            f"{result}\n\n"
            f"📦  Import in your web project:\n"
            f"    // Three.js\n"
            f"    import {{ GLTFLoader }} from 'three/addons/loaders/GLTFLoader.js'\n"
            f"    const loader = new GLTFLoader()\n"
            f"    loader.load('/{filename}.{ext}', (gltf) => scene.add(gltf.scene))\n\n"
            f"    // React Three Fiber\n"
            f"    import {{ useGLTF }} from '@react-three/drei'\n"
            f"    const {{ scene }} = useGLTF('/{filename}.{ext}')"
        )
    return result


@mcp.tool()
def render_viewport_screenshot(output_path: str, resolution_x: int = 1920, resolution_y: int = 1080) -> str:
    """
    Renders a screenshot of the current Blender viewport to a PNG file.

    Parameters
    ----------
    output_path  : Absolute path for the PNG output (e.g. "/tmp/render.png")
    resolution_x : Render width in pixels (default 1920)
    resolution_y : Render height in pixels (default 1080)
    """
    safe_path = output_path.replace("\\", "/")
    code = textwrap.dedent(f"""
        import os as _os
        _os.makedirs(_os.path.dirname("{safe_path}") or ".", exist_ok=True)
        scene = bpy.context.scene
        scene.render.image_settings.file_format = 'PNG'
        scene.render.filepath = "{safe_path}"
        scene.render.resolution_x = {resolution_x}
        scene.render.resolution_y = {resolution_y}
        bpy.ops.render.render(write_still=True)
        _result = "Rendered to: {safe_path}"
    """).strip()
    return _exec_bpy(code, f"render_screenshot → {output_path}")


@mcp.tool()
def undo_last_action(steps: int = 1) -> str:
    """
    Undoes the last N actions in Blender (default: 1).
    Maximum safe undo steps: 32.
    """
    steps = max(1, min(steps, 32))
    code = "\n".join([
        f"bpy.ops.ed.undo()" for _ in range(steps)
    ] + [f'_result = "Undone {steps} step(s)"'])
    return _exec_bpy(code, f"undo({steps})")


@mcp.tool()
def run_raw_bpy(code: str) -> str:
    """
    Executes arbitrary bpy Python code in Blender directly.
    Use this for advanced operations not covered by other tools.

    Safety: Code runs in Blender's Python context. Use with care.

    Tips:
    - Set `_result = "message"` to return a custom success message
    - Append to `_output` list for multi-line results
    - `bpy`, `mathutils`, `os`, `json` are pre-imported in exec scope
    """
    if not code.strip():
        return "❌  Empty code provided."
    return _exec_bpy(code, "run_raw_bpy")


@mcp.tool()
def hello_world_test() -> str:
    """
    Hello World test: clears the default scene, adds a green shiny cube,
    and positions the camera. Verifies the full pipeline end-to-end.
    Run this first after setting up the bridge.
    """
    code = textwrap.dedent("""
        # ── Clear default scene ──────────────────────────────────────────
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)

        # ── Add a cube ───────────────────────────────────────────────────
        bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
        cube = bpy.context.active_object
        cube.name = "MCP_HelloCube"

        # ── Apply a green metallic material ──────────────────────────────
        mat = bpy.data.materials.new("MCP_GreenMetal")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Base Color"].default_value  = (0.0, 0.8, 0.2, 1.0)
        bsdf.inputs["Metallic"].default_value    = 0.9
        bsdf.inputs["Roughness"].default_value   = 0.1
        cube.data.materials.append(mat)

        # ── Add a light ───────────────────────────────────────────────────
        bpy.ops.object.light_add(type='SUN', location=(5, -5, 10))
        bpy.context.active_object.data.energy = 3.0

        # ── Add a camera ─────────────────────────────────────────────────
        bpy.ops.object.camera_add(location=(7, -7, 5))
        cam = bpy.context.active_object
        import math
        cam.rotation_euler = (math.radians(60), 0, math.radians(45))
        bpy.context.scene.camera = cam

        _result = (
            f"Hello World! ✅\\n"
            f"  Cube: {cube.name} at {list(cube.location)}\\n"
            f"  Material: {mat.name}\\n"
            f"  Objects in scene: {len(bpy.context.scene.objects)}"
        )
    """).strip()
    return _exec_bpy(code, "hello_world_test")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 4: MCP RESOURCES (read-only context for the AI agent)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.resource("blender://bpy-cheatsheet")
def bpy_cheatsheet() -> str:
    """Quick-reference bpy API cheatsheet for the AI agent."""
    return textwrap.dedent("""
    # bpy Quick Reference Cheatsheet

    ## Scene & Objects
    bpy.context.scene                        # Active scene
    bpy.context.scene.objects               # All scene objects
    bpy.context.view_layer.objects.active   # Active object
    bpy.data.objects["Cube"]               # Get by name
    obj.select_set(True/False)
    bpy.ops.object.select_all(action='SELECT'|'DESELECT'|'INVERT')

    ## Add Primitives
    bpy.ops.mesh.primitive_cube_add(size=2, location=(x,y,z))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=(x,y,z))
    bpy.ops.mesh.primitive_cylinder_add(radius=1, depth=2, location=(x,y,z))
    bpy.ops.mesh.primitive_plane_add(size=2, location=(x,y,z))
    bpy.ops.mesh.primitive_torus_add(location=(x,y,z))
    bpy.ops.mesh.primitive_cone_add(vertices=32, location=(x,y,z))
    bpy.ops.curve.primitive_bezier_curve_add(location=(x,y,z))

    ## Transform
    obj.location    = (x, y, z)
    obj.scale       = (sx, sy, sz)
    obj.rotation_euler = (rx, ry, rz)  # radians — use math.radians(deg)

    ## Modifiers
    mod = obj.modifiers.new("Subsurf", 'SUBSURF')
    mod.levels = 2
    bpy.ops.object.modifier_apply(modifier=mod.name)

    ## Materials (PBR)
    mat = bpy.data.materials.new("MyMat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value   = (R, G, B, A)  # 0.0–1.0
    bsdf.inputs["Metallic"].default_value     = 0.0–1.0
    bsdf.inputs["Roughness"].default_value    = 0.0–1.0
    bsdf.inputs["Alpha"].default_value        = 0.0–1.0
    obj.data.materials.append(mat)

    ## Lights
    bpy.ops.object.light_add(type='POINT'|'SUN'|'SPOT'|'AREA', location=(x,y,z))
    light = bpy.context.active_object.data
    light.energy = 100
    light.color  = (R, G, B)

    ## Camera
    bpy.ops.object.camera_add(location=(x,y,z))
    cam = bpy.context.active_object
    bpy.context.scene.camera = cam

    ## Export
    bpy.ops.export_scene.gltf(filepath="/abs/path/scene.glb", export_format='GLB')

    ## Delete
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

    ## Returning Results
    _result = "Your message here"   # Single result
    _output.append("line 1")        # Multi-line output
    """).strip()


@mcp.resource("blender://connection-status")
def connection_status() -> str:
    """Current connection status to Blender listener."""
    try:
        result = _send_to_blender({"action": "ping"}, timeout=3.0)
        if result.get("status") == "pong":
            return json.dumps({
                "connected": True,
                "blender_version": result.get("blender_version"),
                "server_version": result.get("server_version"),
                "host": BLENDER_HOST,
                "port": BLENDER_PORT,
            })
        return json.dumps({"connected": False, "reason": "Unexpected response"})
    except Exception as e:
        return json.dumps({"connected": False, "reason": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("═" * 60)
    log.info(f"  Blender MCP Bridge Server v{SERVER_VERSION}")
    log.info(f"  Blender target: {BLENDER_HOST}:{BLENDER_PORT}")
    log.info("═" * 60)
    mcp.run()
