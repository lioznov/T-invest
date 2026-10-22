"""Own only the analysis child started by this collector; never touch other PIDs."""
import subprocess
import sys
import uuid
import os
from pathlib import Path


class SignalWorker:
    def __init__(self, tickers, seconds, base):
        self.tickers, self.seconds, self.base = tickers, seconds, Path(base).resolve()
        self.process, self.output = None, None

    def start(self, directory):
        self.output = (Path(directory)/("analysis-"+uuid.uuid4().hex[:8]+".log")).open("x", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-u", str(self.base/"manage.py"), "monitor_signals", "--tickers", *self.tickers,
                 "--interval", "30", "--seconds", str(self.seconds)], cwd=self.base,
                stdout=self.output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            self.output.close()
            raise

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.alive:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.output is not None:
            self.output.close()
