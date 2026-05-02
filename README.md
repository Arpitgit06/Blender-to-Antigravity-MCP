# Blender ↔ AntiGravity MCP Bridge — Setup Guide

A production-grade MCP system that gives your AI agent direct, real-time control
over a live Blender instance for 3D asset generation, modification, and web export.

---

## Architecture Overview

```
AntiGravity IDE (AI Agent)
         │  MCP Protocol (stdio)
         ▼
  ┌─────────────────┐
  │  mcp_server.py  │  ← FastMCP middleware (your system Python)
  └────────┬────────┘
           │  TCP Socket  127.0.0.1:9876
           │  Length-prefixed JSON protocol
           ▼
  ┌──────────────────────┐
  │  blender_listener.py │  ← Runs inside Blender's Script tab
  │  (background thread) │
  │         │            │
  │  ┌──────▼──────┐     │
  │  │ command_queue│    │
  │  └──────┬──────┘     │
  │         │            │
  │  bpy.app.timers      │  ← Main thread executor
  │  (main thread)       │
  └──────────────────────┘
```

**Critical design**: All `bpy` calls execute on Blender's main thread via a timer
callback. The socket server runs in a background thread and communicates through
a thread-safe queue. This is the ONLY safe architecture for Blender.

---

## Prerequisites

| Component | Requirement |
|-----------|-------------|
| Blender   | 3.x or 4.x (tested on 3.6 LTS and 4.1) |
| Python    | 3.9+ (system — for MCP server) |
| OS        | macOS, Windows, Linux |
| AntiGravity IDE | Any version supporting MCP config |

---

## Step 1 — Install System Dependencies

```bash
# Navigate to the bridge folder
cd /path/to/blender_mcp_bridge

# Install MCP server dependencies
pip install -r requirements.txt

# Verify
python -c "from mcp.server.fastmcp import FastMCP; print('FastMCP OK')"
```

---

## Step 2 — Blender Internal Python (no pip needed)

The `blender_listener.py` script uses ONLY Python standard library modules:
`socket`, `threading`, `queue`, `json`, `traceback`, `logging`, `os`, `time`

These are all included with Blender's bundled Python — **no pip install needed**
inside Blender.

> ⚠️ If you ever DO need to pip install inside Blender, find Blender's Python:
> - **macOS**: `/Applications/Blender.app/Contents/Resources/4.x/python/bin/python3.xx`
> - **Windows**: `C:\Program Files\Blender Foundation\Blender 4.x\4.x\python\bin\python.exe`
> - **Linux**: `/usr/share/blender/4.x/python/bin/python3.xx`
>
> Then run: `/path/to/blender/python -m pip install <package>`

---

## Step 3 — Start the Blender Listener

1. Open **Blender**
2. Go to **Scripting** workspace (tab at the top)
3. Click **New** (or Open Text) in the text editor panel
4. Open `blender_listener.py` from this folder
5. Click **▶ Run Script** (or press `Alt+P`)
6. Open the **System Console** to verify:
   - Windows: `Window → Toggle System Console`
   - macOS/Linux: launch Blender from terminal: `/Applications/Blender.app/Contents/MacOS/Blender`

You should see:
```
════════════════════════════════════════════════════════════
  Blender MCP Listener v2.0.0 STARTED
  Listening on 127.0.0.1:9876
  Log file: ~/blender_mcp_listener.log
════════════════════════════════════════════════════════════
```

---

## Step 4 — Configure AntiGravity IDE

Edit `mcp_config.json` and replace the path placeholder:

```json
{
  "mcpServers": {
    "blender-bridge": {
      "command": "python",
      "args": ["/ABSOLUTE/PATH/TO/blender_mcp_bridge/mcp_server.py"]
    }
  }
}
```

**macOS example**:
```json
"args": ["/Users/yourname/projects/blender_mcp_bridge/mcp_server.py"]
```

**Windows example**:
```json
"command": "python",
"args": ["C:/Users/yourname/projects/blender_mcp_bridge/mcp_server.py"]
```

> ⚠️ **ALWAYS use absolute paths**. Relative paths fail because AntiGravity's
> working directory is unpredictable. This is the #1 cause of the
> `No workspace window available` error.

---

## Step 5 — Verify the Pipeline

```bash
python test_connection.py
```

Expected output:
```
✅ PASS  Ping (connection check)
✅ PASS  Hello World — green cube
✅ PASS  Scene info query
✅ PASS  Transform object (move/rotate/scale)
✅ PASS  Error handling (invalid code → clean error)
✅ PASS  Multi-line _output result
✅ PASS  Python stdlib + bpy interop

Passed: 7/7
🎉  All tests passed! Your pipeline is ready.
```

---

## Hello World Command (from AntiGravity)

Once connected, ask your AI agent:

> "Run the hello world test in Blender"

Or directly call the `hello_world_test` MCP tool. You'll see a shiny green
metallic cube appear in Blender's viewport.

---

## Example AI Agent Prompts

```
"Add a red metallic sphere at position (0, 0, 2) in Blender"

"Create a low-poly tree with a brown trunk and green leaves"

"Apply a glass material to the selected object"

"Export the current scene as scene.glb into /Users/me/myapp/public"

"What objects are currently in the Blender scene?"

"Move Cube to (3, 0, 0) and rotate it 45 degrees on the Z axis"
```

---

## Troubleshooting

### ❌ Connection refused on port 9876

- Blender is not open, OR
- `blender_listener.py` was not run, OR
- A previous crash left the port bound — restart Blender

### ❌ "No workspace window available"

This is a Blender context error when `bpy.ops` tries to run without a viewport.
Fix: Launch Blender normally (with a viewport), not headlessly.
The listener script requires a full Blender GUI session.

### ❌ Port already in use

```bash
# macOS / Linux — find and kill the process using 9876
lsof -ti:9876 | xargs kill -9

# Windows PowerShell
netstat -ano | findstr 9876
taskkill /PID <PID> /F
```

Then re-run the listener script in Blender.

### ❌ bpy.context.active_object is None

You must set the active object explicitly before using context-dependent ops:
```python
bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]
```

### ❌ Export failed / directory not found

`trigger_web_export` creates directories automatically. Ensure the path is:
- Absolute (not relative)
- Uses forward slashes or escaped backslashes
- The disk has write permissions

### ❌ Blender hangs / freezes

The listener's 60-second execution timeout will return an error to the MCP
server. Long operations (complex renders, large exports) may need EXEC_TIMEOUT
increased in mcp_server.py.

### ❌ Timer not processing commands

If Blender is in a modal state (e.g., you're actively transforming an object
with G/R/S), the timer pauses. Finish the current interaction first.

---

## File Structure

```
blender_mcp_bridge/
├── blender_listener.py   ← Paste and run inside Blender's Scripting tab
├── mcp_server.py         ← FastMCP server (registered in AntiGravity)
├── mcp_config.json       ← AntiGravity IDE configuration
├── test_connection.py    ← End-to-end pipeline test suite
├── requirements.txt      ← pip packages for system Python
└── README.md             ← This file
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BLENDER_MCP_HOST` | `127.0.0.1` | Blender listener host |
| `BLENDER_MCP_PORT` | `9876` | Blender listener port |
| `BLENDER_CONNECT_TIMEOUT` | `5.0` | Seconds to wait for connection |
| `BLENDER_EXEC_TIMEOUT` | `60.0` | Seconds to wait for bpy execution |

Override in `mcp_config.json`'s `env` block.

---

## Security Notes

- The listener only binds to `127.0.0.1` (localhost) — not exposed on network
- `exec()` is intentionally used for flexibility — this is a local dev tool
- Do not expose port 9876 through firewalls or port-forwarding
