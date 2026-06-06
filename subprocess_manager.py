import os
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from tempfile import TemporaryFile
from typing import Optional

from PyQt6.QtCore import QThread, QObject, pyqtSignal, Qt


@dataclass
class ConversationState:
    thread: Optional[QThread]
    worker: "Worker"
    running: bool
    process_handle: Optional[subprocess.Popen] = field(default=None)


class Worker(QObject):
    signal_stderr_chunk = pyqtSignal(object, str)
    signal_completed = pyqtSignal(object, str, object)
    signal_error = pyqtSignal(object, str)
    signal_finished = pyqtSignal()
    signal_process_handle = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._command: list[str] = []
        self._prompt: str = ""
        self._session_id: Optional[str] = None
        self._working_directory: Optional[str] = None
        self._conversation_id: Optional[str] = None
        self._kill_requested: bool = False

    def execute(self) -> None:
        command = self._command
        prompt = self._prompt
        session_id = self._session_id
        cwd_setting = self._working_directory
        conversation_id = self._conversation_id

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

            # Store process handle and emit it for the manager
            self._process_handle = process
            self.signal_process_handle.emit(process)

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
                            self.signal_stderr_chunk.emit(conversation_id, decoded)
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

            if not stdout:
                raise RuntimeError(combined_stderr[-1024:])

            self.signal_completed.emit(conversation_id, stdout, session_id)
            self.signal_finished.emit()
        except FileNotFoundError:
            try:
                self.signal_error.emit(conversation_id, "codex binary not found")
            except Exception:
                pass
            self.signal_finished.emit()
        except OSError:
            try:
                self.signal_error.emit(conversation_id, "stdin write failure")
            except Exception:
                pass
            self.signal_finished.emit()
        except RuntimeError as e:
            try:
                self.signal_error.emit(conversation_id, str(e))
            except Exception:
                pass
            self.signal_finished.emit()
        except Exception:
            try:
                self.signal_error.emit(conversation_id, "Unknown error")
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


COOPERATIVE_TIMEOUT = 2.0
THREAD_WAIT_TIMEOUT = 3.0


class SubprocessManager(QObject):
    signal_stderr_chunk = pyqtSignal(object, str)
    signal_completed = pyqtSignal(object, str, object)
    signal_error = pyqtSignal(object, str)

    def __init__(
        self,
        schema: dict,
        timeout: Optional[str] = None,
        persistence=None,
    ) -> None:
        super().__init__()
        self._schema = schema
        self._timeout = timeout
        self._persistence = persistence
        self._conversations: dict[str, ConversationState] = {}
        self._lifecycle_listeners: list = []

    def _build_command(
        self, session_id: Optional[str], binary_path: Optional[str] = None
    ) -> list[str]:
        cmd = []
        if self._timeout:
            cmd.extend(["timeout", "-s", "9", self._timeout])
        if shutil.which("stdbuf"):
            cmd.extend(["stdbuf", "-eL"])

        if binary_path:
            cmd.extend([binary_path, "exec"])
        else:
            cmd.extend(["claude", "exec"])

        if session_id:
            cmd.extend(["resume", session_id])
        return cmd

    def _terminate_conversation(self, conversation_id: str) -> None:
        """Dual-path termination: cooperative kill then forcible Popen.kill()."""
        entry = self._conversations.get(conversation_id)
        if entry is None:
            return

        # Stage 1: Cooperative kill
        entry.worker._kill_requested = True
        entry.thread.wait(int(COOPERATIVE_TIMEOUT * 1000))

        # Stage 2: Forcible
        if entry.thread.isRunning():
            # Dual-source process handle resolution (handles signal delivery race)
            process_handle = entry.process_handle or getattr(entry.worker, '_process_handle', None)
            if process_handle is not None:
                try:
                    process_handle.kill()
                    process_handle.wait()
                except Exception:
                    pass
            entry.thread.wait(int(THREAD_WAIT_TIMEOUT * 1000))

        entry.running = False

        # Guarded thread cleanup
        if entry.thread.isRunning():
            entry.thread.quit()
            entry.thread.wait()
            entry.thread.deleteLater()

    def _handle_thread_finished(self) -> None:
        """Backup cleanup for crashed/hung workers where signal_finished never fires."""
        sender_thread = self.sender()
        for cid, entry in list(self._conversations.items()):
            if entry.thread is sender_thread:
                if not entry.running:
                    return  # Already cleaned up by stop() or _handle_worker_finished
                entry.running = False
                if entry.thread.isRunning():
                    entry.thread.quit()
                    entry.thread.wait()
                    entry.thread.deleteLater()
                del self._conversations[cid]
                return

    def _handle_worker_finished(self) -> None:
        """Safety net: cleanup when signal_finished fires but stop() was not called."""
        sender_worker = self.sender()
        for cid, entry in list(self._conversations.items()):
            if entry.worker is sender_worker:
                if not entry.running:
                    return  # Already cleaned by stop()
                entry.running = False
                if entry.thread.isRunning():
                    entry.thread.quit()
                    entry.thread.wait()
                    entry.thread.deleteLater()
                del self._conversations[cid]
                return

    def submit(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        if not conversation_id:
            return

        # Prevent duplicate subprocesses in the same conversation
        if conversation_id in self._conversations:
            entry = self._conversations[conversation_id]
            if entry.running:
                self.signal_error.emit(None, "Another subprocess is already running")
                return

        # Resolve subprocess parameters for this conversation
        binary_path = None
        working_directory = None
        if self._persistence is not None:
            resolved = self._persistence.resolve_subprocess_parameters(conversation_id)
            binary_path = resolved.get("binary_path")
            working_directory = resolved.get("working_directory")

        command = self._build_command(session_id, binary_path)
        worker = Worker()
        worker._command = command
        worker._prompt = prompt
        worker._session_id = session_id
        worker._working_directory = working_directory
        worker._conversation_id = conversation_id
        worker._kill_requested = False

        thread = QThread()

        entry = ConversationState(
            thread=None,
            worker=worker,
            running=True,
            process_handle=None,
        )

        # Forward stderr/completed/error via lambdas
        worker.signal_stderr_chunk.connect(
            lambda cid, chunk: self.signal_stderr_chunk.emit(cid, chunk),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.signal_completed.connect(
            lambda cid, stdout, sid: self.signal_completed.emit(cid, stdout, sid),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.signal_error.connect(
            lambda cid, msg: self.signal_error.emit(cid, msg),
            Qt.ConnectionType.QueuedConnection,
        )
        # Process handle transfer (emitted after Popen creation)
        worker.signal_process_handle.connect(
            lambda h: setattr(entry, 'process_handle', h),
            Qt.ConnectionType.QueuedConnection,
        )
        # Worker finished -> safety net cleanup (no forwarding to SubprocessManager)
        worker.signal_finished.connect(
            self._handle_worker_finished,
            Qt.ConnectionType.QueuedConnection,
        )

        # Thread finished -> backup cleanup (AutoConnection since both live in main thread)
        thread.finished.connect(self._handle_thread_finished)

        worker.moveToThread(thread)
        thread.started.connect(worker.execute)

        entry.thread = thread
        self._conversations[conversation_id] = entry
        thread.start()
        self.notify_lifecycle(conversation_id, 'start')

    def is_running(self) -> bool:
        """Check if any conversation has a running subprocess."""
        for entry in self._conversations.values():
            if entry.running:
                return True
        return False

    def stop(self, conversation_id: Optional[str] = None) -> None:
        """Stop a specific conversation's subprocess, or the active one if not specified."""
        if conversation_id is None:
            if self._persistence is not None:
                conversation_id = self._persistence.get_active_conversation_id()
            else:
                return

        entry = self._conversations.get(conversation_id)
        if entry is None:
            return
        if not entry.running:
            return

        self._terminate_conversation(conversation_id)
        del self._conversations[conversation_id]

    def reset_active_state(self) -> None:
        """Terminate all running subprocesses and clear all conversation state."""
        cids = list(self._conversations.keys())
        for cid in cids:
            self._terminate_conversation(cid)
        self._conversations.clear()


    def is_conversation_running(self, cid: str) -> bool:
        """Check if a specific conversation has a running subprocess."""
        entry = self._conversations.get(cid)
        if entry is None:
            return False
        return entry.running

    def register_lifecycle_listener(self, delegate) -> None:
        """Register a lifecycle listener delegate."""
        self._lifecycle_listeners.append(delegate)

    def notify_lifecycle(self, cid: str, event: str) -> None:
        """Notify all registered listeners about a lifecycle event."""
        for listener in self._lifecycle_listeners:
            listener.on_lifecycle_notification(cid, event)

    def has_conversation(self, cid: str) -> bool:
        """Check if a conversation exists in _conversations."""
        return cid in self._conversations


    def get_running_conversation_ids(self) -> list[str]:
        """Return CIDs of conversations with a running subprocess.

        entry.running alone is sufficient here because _terminate_conversation
        handles non-running entries gracefully (it checks entry.running internally).
        """
        return [cid for cid, entry in self._conversations.items() if entry.running]

    def clear_all(self) -> None:
        """Remove all conversation entries from _conversations.

        Required because _terminate_conversation only sets running=False and
        does not delete entries; _handle_thread_finished fires asynchronously
        after thread.wait() returns.
        """
        self._conversations.clear()

    def detect_orphaned_subprocesses(self) -> list[str]:
        """Detect and terminate subprocesses whose conversations no longer exist in persistence.

        Uses snapshot iteration (list(self._conversations.keys())) matching the
        pattern in reset_active_state at line 360.  Compares _conversations keys
        against DataPersistenceManager._conversations keys.  Terminates orphaned
        entries via _terminate_conversation with notify_lifecycle(cid, 'orphan').

        Returns a list of orphaned CID strings for informational purposes. Callers may safely ignore the return value since detect_orphaned_subprocesses performs termination and lifecycle notification as side effects.

        The 'orphan' event is silently ignored by InputBarStateDelegate because
        no UI state change is needed — the conversation is already gone from
        persistence.
        """
        persistent_cids = set()
        if self._persistence is not None:
            persistent_cids = set(self._persistence._conversations.keys())

        orphan_cids = []
        for cid in list(self._conversations.keys()):
            if cid not in persistent_cids:
                self._terminate_conversation(cid)
                self.notify_lifecycle(cid, 'orphan')
                orphan_cids.append(cid)
        return orphan_cids

    def terminate_running_conversations(self) -> None:
        """Terminate all conversations with a running subprocess.

        Composes get_running_conversation_ids() with _terminate_conversation
        to provide a public encapsulation for the termination loop.
        """
        for cid in self.get_running_conversation_ids():
            self._terminate_conversation(cid)
