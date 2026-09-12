# Intelligent Compiler

A FastAPI application with a Monaco editor, interactive terminal, syntax diagnostics,
and optional AI suggestions through NVIDIA NIM. Executes Python, C, C++, Java, and
JavaScript; HTML runs in a sandboxed browser preview.

## Local development

Use Linux with Python 3.11+, Node.js, GCC, G++, a JDK (javac and java), and
stdbuf (coreutils). From the repository root:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app:app --host 127.0.0.1 --port 10000
```

Open http://localhost:10000. The UI uses its serving origin for HTTP and WebSocket
requests. GitHub Pages uses the configured Render backend. Opening index.html
directly uses localhost:10000.

Set NVIDIA_API_KEY before starting the server for AI suggestions. NIM_MODEL
optionally overrides the identifier in app.py; choose a model available to your
NVIDIA account. Without a key, editing, linting, and execution still work; the AI
endpoint returns HTTP 503. No .env file is loaded automatically.

## Verification

```sh
venv/bin/pip install httpx
venv/bin/python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
venv/bin/python -m pyflakes app.py tests/test_app.py
```

Runtime tests require the corresponding compilers; unavailable runtimes are skipped.
AI tests use a fake provider and do not send code externally.

## Deployment boundary

The current execution service is intended for trusted local use. Resource limits,
timeouts, process groups, and temporary directories are **not an execution sandbox**.
Submitted programs can access the server filesystem, environment, and network.
The Dockerfile also runs the application as root. Isolate runners from the API and
its secrets before exposing the execution service to untrusted users.

See [REVIEW.md](REVIEW.md) for findings and improvement priorities.
