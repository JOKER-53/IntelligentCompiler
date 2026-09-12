import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)

    def test_home_uses_application_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                response = self.client.get("/")
            finally:
                os.chdir(previous)
        self.assertEqual(response.status_code, 200)
        self.assertIn("monaco-container", response.text)

    def test_ai_key_is_optional(self):
        with patch.object(app, "nim_client", None):
            response = self.client.post("/analyze", json={"lang": "python", "code": "pass", "output": ""})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.post("/lint", json={"lang": "python", "code": "pass"}).json(), {"diagnostics": []})

    def run_code(self, lang, code, stdin=None):
        chunks = []
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"lang": lang, "code": code})
            if stdin is not None:
                ws.send_json({"stdin": stdin})
            with self.assertRaises(WebSocketDisconnect) as closed:
                while True:
                    chunks.append(ws.receive_text())
            self.assertEqual(closed.exception.code, 1000)
        return "".join(chunks)

    def test_python_input_and_clean_close(self):
        self.assertEqual(self.run_code("python", 'print("hello " + input())', "world"), "hello world\n")

    def test_supported_runtimes(self):
        examples = [
            ("js", "node", 'console.log("hello")'),
            ("c", "gcc", '#include <stdio.h>\nint main(){puts("hello");}'),
            ("cpp", "g++", '#include <iostream>\nint main(){std::cout << "hello\\n";}'),
            ("java", "javac", 'package example;\npublic class Hello {public static void main(String[] args){System.out.println("hello");}}'),
        ]
        for lang, tool, code in examples:
            with self.subTest(lang=lang):
                if shutil.which(tool):
                    self.assertEqual(self.run_code(lang, code), "hello\n")

    def test_invalid_request_closes(self):
        self.assertIn("must be strings", self.run_code("python", 42))

    def test_compiler_failure_closes(self):
        if not shutil.which("gcc"):
            self.skipTest("gcc unavailable")
        self.assertIn("error:", self.run_code("c", "int main( {"))

    def test_interrupt_stops_process(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"lang": "python", "code": 'import time; print("ready", flush=True); time.sleep(30)'})
            self.assertIn("ready", ws.receive_text())
            ws.send_json({"signal": "SIGINT"})
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_text()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_javascript_lint_never_executes_code(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "executed"
            diagnostics = await app._lint_js(f'require("fs").writeFileSync({str(marker)!r}, "bad");')
            self.assertEqual(diagnostics, [])
            self.assertFalse(marker.exists())
        self.assertTrue(await app._lint_js("const x = ;"))

    async def test_timeout_reaps_process(self):
        proc = await app.start_process(sys.executable, "-c", "import time; time.sleep(30)", stdout=subprocess.PIPE)
        with self.assertRaises(asyncio.TimeoutError):
            await app.communicate(proc, timeout=0.05)
        self.assertIsNotNone(proc.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(proc.pid, 0)

    async def test_disconnect_terminates_execution(self):
        class DisconnectedSocket:
            async def receive_json(self):
                raise WebSocketDisconnect()

            async def send_text(self, text):
                pass

        await asyncio.wait_for(app.run_interactive_process(
            DisconnectedSocket(), [sys.executable, "-c", "import time; time.sleep(30)"]
        ), timeout=2)

    async def test_ai_does_not_block_event_loop(self):
        import threading
        from types import SimpleNamespace
        started = threading.Event()
        release = threading.Event()

        def create(**kwargs):
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(app, "nim_client", fake):
            task = asyncio.create_task(app.analyze_code(app.AnalyzeRequest(code="pass", lang="python", output="")))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set())
                self.assertFalse(task.done(), "AI request blocked the event loop")
            finally:
                release.set()
                result = await task
            self.assertEqual(result, {"suggestion": "ok"})


if __name__ == "__main__":
    unittest.main()
