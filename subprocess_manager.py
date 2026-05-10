import os
import re
import select
import shutil
import subprocess
import time
from tempfile import TemporaryFile
from typing import Optional

from PyQt6.QtCore import QThread, QObject, pyqtSignal, Qt

from settings import read_settings


class Worker(QObject):
    signal_stderr_chunk = pyqtSignal(str)
    signal_completed = pyqtSignal(str, object)
    signal_error = pyqtSignal(str)
    signal_finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._command: list[str] = []
        self._prompt: str = ""
        self._session_id: Optional[str] = None
        self._kill_requested = False

    def execute(self) -> None:
        command = self._command
        prompt = self._prompt
        session_id = self._session_id

        # Resolve working directory from settings
        settings = read_settings()
        cwd_setting = settings.get("working_directory")

        process = None
        try:
            # Change to working directory before spawning, restore immediately after
            _saved_cwd = None
            if cwd_setting:
                _saved_cwd = os.getcwd()
                os.chdir(cwd_setting)

            with TemporaryFile(buffering=0) as prompt_file:
                prompt_file.write(prompt.encode("utf-8"))
                prompt_file.seek(0)

                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=prompt_file,
                    env=os.environ,
                )

            # Restore original working directory immediately after spawn
            if _saved_cwd is not None:
                try:
                    os.chdir(_saved_cwd)
                except OSError:
                    pass
            # prompt_file closed; Popen has duplicated the fd

            combined_stderr = ""
            combined_stdout = ""
            stderr_fd = process.stderr.fileno()
            stdout_fd = process.stdout.fileno()
            fds = [stdout_fd, stderr_fd]
            eof = {stdout_fd: False, stderr_fd: False}

            while not self._kill_requested:
                active = [fd for fd in fds if not eof[fd]]
                if not active:
                    break
                try:
                    ready, _, _ = select.select(active, [], [], 0.1)
                except ValueError:
                    break
                if not ready:
                    continue
                for fd in ready:
                    try:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            eof[fd] = True
                            continue
                        if fd == stderr_fd:
                            decoded = chunk.decode("utf-8", errors="replace")
                            self.signal_stderr_chunk.emit(decoded)
                            combined_stderr += decoded
                        else:
                            combined_stdout += chunk.decode("utf-8", errors="replace")
                    except Exception:
                        eof[fd] = True

            if not self._kill_requested:
                process.wait()

            stdout = combined_stdout.strip()

            session_id = None
            if match := re.search(r"session id:\s*(\S*)", combined_stderr):
                session_id = match.group(1) or None

            if session_id:
                self.signal_completed.emit(stdout, session_id)

            if not stdout:
                raise RuntimeError("Empty output")
        except FileNotFoundError:
            try:
                self.signal_error.emit("codex binary not found")
            except Exception:
                pass
            self.signal_finished.emit()
        except OSError:
            try:
                self.signal_error.emit("stdin write failure")
            except Exception:
                pass
            self.signal_finished.emit()
        except RuntimeError:
            try:
                self.signal_error.emit("Empty output")
            except Exception:
                pass
            self.signal_finished.emit()
        except KeyboardInterrupt:
            # Kill was requested via Ctrl+C – just clean up and finish silently
            self.signal_finished.emit()
        except Exception as e:
            try:
                self.signal_error.emit(str(e))
            except Exception:
                pass
            self.signal_finished.emit()
        finally:
            try:
                if process is not None and process.returncode is None:
                    process.kill()
                    process.wait()
            except:
                pass


class SubprocessManager(QObject):
    signal_stderr_chunk = pyqtSignal(str)
    signal_completed = pyqtSignal(str, object)
    signal_error = pyqtSignal(str)
    signal_finished = pyqtSignal()
    signal_kill_requested = pyqtSignal()

    def __init__(self, schema: dict, timeout: Optional[str] = None) -> None:
        super().__init__()
        self._schema = schema
        self._timeout = timeout
        self._running = False
        self._thread: Optional[QThread] = None

    def _build_command(self, session_id: Optional[str]) -> list[str]:
        cmd = []
        if self._timeout:
            cmd.extend(["timeout", "-s", "9", self._timeout])
        if shutil.which("stdbuf"):
            cmd.extend(["stdbuf", "-eL"])

        # Read settings for custom binary path (every call to pick up changes)
        settings = read_settings()
        binary_path = settings.get("binary_path")

        if binary_path:
            cmd.extend([binary_path, "exec"])
        else:
            cmd.extend(["claude", "exec"])

        if session_id:
            cmd.extend(["resume", session_id])
        return cmd

    def _handle_worker_finished(self) -> None:
        self.signal_kill_requested.disconnect()
        self._running = False
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        thread.quit()
        thread.wait()
        thread.deleteLater()

    def submit(self, prompt: str, session_id: Optional[str] = None) -> None:
        if self._running:
            self.signal_error.emit("Another subprocess is already running")
            return

        command = self._build_command(session_id)
        worker = Worker()
        worker._command = command
        worker._prompt = prompt
        worker._session_id = session_id

        thread = QThread()

        worker.signal_stderr_chunk.connect(
            self.signal_stderr_chunk.emit, Qt.ConnectionType.QueuedConnection
        )
        worker.signal_completed.connect(
            lambda stdout, sid: self.signal_completed.emit(stdout, sid),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.signal_error.connect(
            self.signal_error.emit, Qt.ConnectionType.QueuedConnection
        )
        worker.signal_finished.connect(
            self.signal_finished.emit, Qt.ConnectionType.QueuedConnection
        )
        worker.signal_finished.connect(self._handle_worker_finished, Qt.ConnectionType.QueuedConnection)
        self.signal_kill_requested.connect(
            lambda: setattr(worker, "_kill_requested", True),
            Qt.ConnectionType.QueuedConnection,
        )
        thread.finished.connect(worker.deleteLater)
        worker.moveToThread(thread)
        thread.started.connect(worker.execute)

        self._thread = thread
        thread.start()
        self._running = True

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        """Stop the subprocess manager and release the running lock."""
        self._running = False
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
