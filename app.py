from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import subprocess
import tempfile
import uuid
import os
import shutil
import re
import resource
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION ---
# Get your free key at https://build.nvidia.com  (no credit card needed)
# Set env var:  NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxx
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
if not NVIDIA_API_KEY:
    raise RuntimeError("NVIDIA_API_KEY is missing. Get a free key at https://build.nvidia.com")

# OpenAI-compatible client pointed at NVIDIA NIM
nim_client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=NVIDIA_API_KEY,
)

# Best free coding model on NIM — swap to any NIM model string to change
NIM_MODEL = "meta/llama-3.1-8b-instruct"

# --- SECURITY: Resource limits applied to every subprocess ---
def set_resource_limits():
    """
    Minimal safe resource limits for execution subprocesses.
    - RLIMIT_AS removed: causes crashes on Render (Python, Java, Node all need variable amounts)
    - RLIMIT_NPROC removed: Node.js needs pthreads (uv_thread_create)
    - Only CPU time is capped — prevents infinite loops
    - File size capped — prevents disk abuse
    """
    try:
        resource.setrlimit(resource.RLIMIT_CPU,   (10, 10))               # 10s CPU — kills infinite loops
        resource.setrlimit(resource.RLIMIT_FSIZE, (5 * 1024 * 1024,) * 2) # 5MB max file writes
    except Exception:
        pass  # Render may not allow setrlimit — silently ignore

EXECUTION_TIMEOUT = 10  # seconds

# --- MODELS ---
class AnalyzeRequest(BaseModel):
    code: str
    lang: str
    output: str

class LintRequest(BaseModel):
    code: str
    lang: str


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.get("/")
async def serve_ui():
    return FileResponse("index.html")


@app.post("/analyze")
async def analyze_code(req: AnalyzeRequest):
    prompt = f"""
You are a senior software engineer and code reviewer. Analyze the code below with precision.
Language: {req.lang}

User Code:
```{req.lang}
{req.code}
```

Execution Output:
{req.output}

Respond using EXACTLY this structure. Be specific — no generic advice like "add validation" or "use a library":

## 🔍 Analysis
2-3 sentences. State the SPECIFIC bug or issue (if any). If no bug, identify the most impactful improvement. Reference actual line numbers or variable names from the code.

## ⏱ Complexity
- **Time:** O(?) — explain WHY based on the actual code (loops, recursion, built-ins used)
- **Space:** O(?) — explain WHY based on actual variables/structures
- **Can we do better?** Yes/No — if yes, name the specific algorithm or data structure that would improve it

## ✅ Fixed / Improved Code
ONE complete version. Rules:
- Mark ONLY lines you changed with `# FIXED: <specific reason>` (Python/JS) or `// FIXED: <specific reason>` (C/C++/Java)
- The reason must be specific: not "handle error" but "catch ValueError when user enters non-integer"
- Do NOT add unnecessary changes — if a line is correct, leave it as-is with no comment

```{req.lang}
<complete code here>
```

## 💡 Three Specific Improvements
Each must be actionable and reference the actual code:
1. **[Category]** Specific suggestion referencing actual function/variable names — why it matters
2. **[Category]** Specific suggestion — why it matters  
3. **[Category]** Specific suggestion — why it matters

Categories to choose from: [Security] [Performance] [Readability] [Edge Case] [Memory] [Error Handling] [Best Practice]
"""
    try:
        response = nim_client.chat.completions.create(
            model=NIM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            temperature=0.2,
        )
        return {"suggestion": response.choices[0].message.content}
    except Exception as e:
        return {"suggestion": f"AI Error: {str(e)}"}


@app.post("/lint")
async def lint_code(req: LintRequest):
    """
    Real-time syntax/lint check. Returns a list of diagnostics:
    [{ line, col, endLine, endCol, message, severity }]
    severity: "error" | "warning"
    """
    lang = req.lang.lower()
    code = req.code

    if not code.strip():
        return {"diagnostics": []}

    try:
        if lang == "python":
            return {"diagnostics": await _lint_python(code)}
        elif lang == "js" or lang == "javascript":
            return {"diagnostics": await _lint_js(code)}
        elif lang == "c":
            return {"diagnostics": await _lint_c(code)}
        elif lang in ("cpp", "c++"):
            return {"diagnostics": await _lint_cpp(code)}
        elif lang == "java":
            return {"diagnostics": await _lint_java(code)}
        else:
            return {"diagnostics": []}
    except Exception as e:
        return {"diagnostics": [], "error": str(e)}


# ─────────────────────────────────────────────
#  LINTERS
# ─────────────────────────────────────────────

async def _lint_python(code: str):
    """
    Multi-pass linter: collects ALL syntax errors across all lines.
    Strategy: find syntax error on line N → blank that line → re-run → repeat.
    Warnings (undefined names, unused imports) are all reported in one pass.
    """
    diagnostics = []
    seen = set()
    working_code = code

    for _pass in range(8):  # up to 8 passes = up to 8 separate syntax errors
        tmp  = tempfile.gettempdir()
        path = os.path.join(tmp, f"lint_{uuid.uuid4().hex}.py")
        with open(path, "w") as f:
            f.write(working_code)

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-m", "pyflakes", path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=8)
        finally:
            if os.path.exists(path):
                os.remove(path)

        found_syntax_error = False

        for stream, severity in [(stderr_b, "error"), (stdout_b, "warning")]:
            raw = stream.decode(errors="replace")
            for m in re.finditer(r':(\d+):(\d+):\s+(.+)', raw):
                line = int(m.group(1))
                col  = int(m.group(2))
                msg  = m.group(3).strip()
                key  = (line, msg)
                if key in seen:
                    continue
                seen.add(key)
                diagnostics.append({
                    "line":     line,
                    "col":      col,
                    "endLine":  line,
                    "endCol":   col + 1,
                    "message":  msg,
                    "severity": severity,
                })
                if severity == "error":
                    found_syntax_error = True
                    # Blank the bad line so next pass finds the next error
                    lines = working_code.split("\n")
                    if line - 1 < len(lines):
                        lines[line - 1] = "pass  # blanked"
                        working_code = "\n".join(lines)

        # If this pass had no syntax errors, all warnings already collected — done
        if not found_syntax_error:
            break

    return sorted(diagnostics, key=lambda d: d["line"])

async def _lint_js(code: str):
    """
    2-pass JS linting:
    Pass 1: node --check  → syntax errors (multi-pass for multiple errors)
    Pass 2: node execution → ReferenceError/TypeError as warnings
    """
    diagnostics = []
    seen = set()
    working = code

    # Pass 1: syntax errors
    for _pass in range(5):
        tmp  = tempfile.gettempdir()
        path = os.path.join(tmp, f"lint_{uuid.uuid4().hex}.js")
        with open(path, "w") as f:
            f.write(working)
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", "--check", path,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        finally:
            if os.path.exists(path): os.remove(path)

        if proc.returncode == 0:
            break

        raw = stderr.decode(errors="replace")
        found = False
        for m in re.finditer(
            r'\.js:(\d+)\n(.*?)\n\s*\^\s*\n+(\w*Error[^\n]*)',
            raw, re.S
        ):
            line = int(m.group(1))
            msg  = m.group(3).strip()
            cm   = re.search(r'\n(\s*)\^', raw[m.start():m.end()])
            col  = len(cm.group(1)) + 1 if cm else 1
            key  = (line, msg)
            if key not in seen:
                seen.add(key)
                diagnostics.append({
                    "line": line, "col": col,
                    "endLine": line, "endCol": col + 1,
                    "message": msg, "severity": "error",
                })
                found = True
                lines = working.split("\n")
                if line - 1 < len(lines):
                    lines[line - 1] = "// blanked"
                    working = "\n".join(lines)
        if not found:
            break

    # Pass 2: runtime errors as warnings (only when no syntax errors)
    if not any(d["severity"] == "error" for d in diagnostics):
        path2 = os.path.join(tempfile.gettempdir(), f"lint_{uuid.uuid4().hex}.js")
        with open(path2, "w") as f:
            f.write(code)
        try:
            proc2 = await asyncio.create_subprocess_exec(
                "node", path2,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            _, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=2)
            raw2 = stderr2.decode(errors="replace")
            # Node runtime: "file.js:LINE\ncode\n   ^\nReferenceError: x is not defined"
            for m in re.finditer(
                r'\.js:(\d+)\n.*?\n\s*\^\s*\n+(ReferenceError|TypeError):\s*(.+)',
                raw2, re.S
            ):
                line  = int(m.group(1))
                msg   = f"{m.group(2)}: {m.group(3).strip()}"
                key   = (line, msg)
                if key not in seen:
                    seen.add(key)
                    diagnostics.append({
                        "line": line, "col": 1,
                        "endLine": line, "endCol": 2,
                        "message": msg, "severity": "warning",
                    })
        except asyncio.TimeoutError:
            pass  # code ran fine or had an infinite loop — ignore
        except Exception:
            pass
        finally:
            if os.path.exists(path2): os.remove(path2)

    return sorted(diagnostics, key=lambda d: d["line"])

async def _lint_c(code: str):
    """Use GCC -fsyntax-only."""
    return await _lint_gcc(code, ["gcc", "-fsyntax-only", "-x", "c", "-Wall", "-"])


async def _lint_cpp(code: str):
    """Use G++ -fsyntax-only."""
    return await _lint_gcc(code, ["g++", "-fsyntax-only", "-x", "c++", "-Wall", "-std=c++17", "-"])


async def _lint_gcc(code: str, cmd: list):
    """
    Generic GCC/G++ linter — reads from stdin.
    Only captures <stdin>: lines to exclude stdlib/header noise.
    Only captures error/warning — skips note: lines.
    Reports ALL errors (GCC reports multiple natively).
    """
    diagnostics = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=code.encode()), timeout=8
        )
        raw = stderr.decode(errors="replace")
        # Match only user-code lines, skip stdlib headers, skip note:
        for m in re.finditer(
            r'^<stdin>:(\d+):(\d+):\s+(error|warning):\s+(.+)',
            raw, re.MULTILINE
        ):
            line     = int(m.group(1))
            col      = int(m.group(2))
            severity = m.group(3)
            msg      = m.group(4).strip()
            msg      = re.sub(r'\s*\[-W[^\]]+\]$', '', msg)  # strip [-Wflag]
            diagnostics.append({
                "line": line, "col": col,
                "endLine": line, "endCol": col + 1,
                "message": msg, "severity": severity,
            })
    except asyncio.TimeoutError:
        pass
    return diagnostics

async def _lint_java(code: str):
    """
    Java linter using javac.
    javac reports ALL errors in one pass natively — no multi-pass needed.
    """
    diagnostics = []
    tmp_dir = os.path.join(tempfile.gettempdir(), f"jlint_{uuid.uuid4().hex}")
    os.makedirs(tmp_dir, exist_ok=True)

    code_stripped = re.sub(r'^\s*package\s+[\w.]+;', '', code, flags=re.MULTILINE)
    match = re.search(r'public\s+class\s+(\w+)', code_stripped) \
         or re.search(r'class\s+(\w+)', code_stripped)
    class_name = match.group(1) if match else "Main"
    src = os.path.join(tmp_dir, f"{class_name}.java")

    with open(src, "w") as f:
        f.write(code_stripped)

    try:
        proc = await asyncio.create_subprocess_exec(
            "javac",
            "-J-Xms32m", "-J-Xmx256m",
            "-J-XX:ReservedCodeCacheSize=32m",
            f"{class_name}.java",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=tmp_dir,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=12)
        raw = stderr.decode(errors="replace")
        # javac: "Filename.java:LINE: error/warning: message"
        for m in re.finditer(r'\.java:(\d+):\s+(error|warning):\s+(.+)', raw):
            line     = int(m.group(1))
            severity = m.group(2)
            msg      = m.group(3).strip()
            diagnostics.append({
                "line": line, "col": 1,
                "endLine": line, "endCol": 2,
                "message": msg, "severity": severity,
            })
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return sorted(diagnostics, key=lambda d: d["line"])




# ─────────────────────────────────────────────
#  WEBSOCKET EXECUTION (with security fixes)
# ─────────────────────────────────────────────

async def run_interactive_process(ws: WebSocket, cmd, cwd=None):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        preexec_fn=set_resource_limits,
    )

    async def stream_output():
        try:
            while True:
                data = await proc.stdout.read(1024)
                if not data:
                    break
                await ws.send_text(data.decode(errors="replace"))
        except Exception:
            pass

    async def stream_input():
        try:
            while True:
                msg = await ws.receive_json()
                if "stdin" in msg and proc.stdin:
                    proc.stdin.write((msg["stdin"] + "\n").encode())
                    await proc.stdin.drain()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    out_task = asyncio.create_task(stream_output())
    in_task  = asyncio.create_task(stream_input())

    try:
        await asyncio.wait_for(out_task, timeout=EXECUTION_TIMEOUT)
    except asyncio.TimeoutError:
        await ws.send_text("\n\x1b[33m[Process timed out after 10 seconds]\x1b[0m\n")
    finally:
        in_task.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def run_python(ws: WebSocket, code: str):
    tmp  = tempfile.gettempdir()
    path = os.path.join(tmp, f"{uuid.uuid4().hex}.py")
    with open(path, "w") as f:
        f.write(code)
    try:
        await run_interactive_process(ws, ["python3", "-u", path])
    finally:
        if os.path.exists(path):
            os.remove(path)


async def run_c(ws: WebSocket, code: str):
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, f"{uuid.uuid4().hex}.c")
    exe = os.path.join(tmp, f"{uuid.uuid4().hex}_c_bin")
    with open(src, "w") as f:
        f.write(code)
    proc = await asyncio.create_subprocess_exec(
        "gcc", src, "-o", exe,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    if out:
        await ws.send_text(out.decode(errors="replace"))
    if proc.returncode != 0:
        if os.path.exists(src): os.remove(src)
        return
    try:
        await run_interactive_process(ws, ["stdbuf", "-o0", "-e0", exe])
    finally:
        for p in (src, exe):
            if os.path.exists(p): os.remove(p)


async def run_cpp(ws: WebSocket, code: str):
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, f"{uuid.uuid4().hex}.cpp")
    exe = os.path.join(tmp, f"{uuid.uuid4().hex}_cpp_bin")
    with open(src, "w") as f:
        f.write(code)
    proc = await asyncio.create_subprocess_exec(
        "g++", src, "-o", exe,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    if out:
        await ws.send_text(out.decode(errors="replace"))
    if proc.returncode != 0:
        if os.path.exists(src): os.remove(src)
        return
    try:
        await run_interactive_process(ws, [exe])
    finally:
        for p in (src, exe):
            if os.path.exists(p): os.remove(p)


async def run_java(ws: WebSocket, code: str):
    tmp_dir = os.path.join(tempfile.gettempdir(), f"java_{uuid.uuid4().hex}")
    os.makedirs(tmp_dir, exist_ok=True)
    code = re.sub(r'^\s*package\s+[\w.]+;', '', code, flags=re.MULTILINE)
    match = re.search(r'public\s+class\s+(\w+)', code) or re.search(r'class\s+(\w+)', code)
    class_name = match.group(1) if match else "Main"
    src = os.path.join(tmp_dir, f"{class_name}.java")
    with open(src, "w") as f:
        f.write(code)
    proc = await asyncio.create_subprocess_exec(
        "javac",
        "-J-Xms32m", "-J-Xmx256m",
        "-J-XX:ReservedCodeCacheSize=32m",
        f"{class_name}.java",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=tmp_dir
    )
    out, _ = await proc.communicate()
    if out:
        await ws.send_text(out.decode(errors="replace"))
    if proc.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return
    try:
        await run_interactive_process(ws, [
            "java",
            "-Xms32m", "-Xmx256m",              # heap: 32MB initial, 256MB max
            "-XX:ReservedCodeCacheSize=32m",    # fix code cache reservation error
            "-XX:TieredStopAtLevel=1",           # disable full JIT — faster startup, less memory
            class_name
        ], cwd=tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def run_js(ws: WebSocket, code: str):
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, f"{uuid.uuid4().hex}.js")
    with open(src, "w") as f:
        f.write(code)
    try:
        await run_interactive_process(ws, ["node", src])
    finally:
        if os.path.exists(src):
            os.remove(src)


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    try:
        first = await ws.receive_json()
        lang  = first.get("lang", "python").lower()
        code  = first.get("code", "")

        if   lang == "python":           await run_python(ws, code)
        elif lang == "c":                await run_c(ws, code)
        elif lang in ("cpp", "c++"):     await run_cpp(ws, code)
        elif lang == "java":             await run_java(ws, code)
        elif lang in ("js", "javascript"): await run_js(ws, code)
        elif lang == "html":             await ws.send_text("HTML is rendered locally.")
        else:                            await ws.send_text(f"Unknown language: {lang}")
    except Exception as e:
        try:
            await ws.send_text(f"\n[Internal error: {str(e)}]")
        except Exception:
            pass
