"""后台进程管理器。

提供异步启动、持续读取输出、以及向交互式进程发送输入的能力。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
DEFAULT_MAX_OUTPUT_CHARS = 256 * 1024


class RunningProcess:
    """包装正在后台运行的子进程。"""

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        shell_name: str,
        session_id: str | None = None,
        *,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ):
        self.id = str(uuid.uuid4())
        self.command = command
        self.cwd = cwd.as_posix()
        self.shell_name = shell_name
        self.session_id = session_id
        self.max_output_chars = max(4096, max_output_chars)
        self.output_buffer: deque[str] = deque()
        self._output_chars = 0
        self.output_discarded_chars = 0
        self._lock = threading.Lock()
        self._cleaned_up = False

        logger.info("Starting process %s: %s in %s session_id=%s", self.id, command, cwd, session_id)
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            bufsize=0,
        )

        self.stdout_thread = threading.Thread(target=self._read_stream, args=(self.process.stdout, "stdout"), daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stream, args=(self.process.stderr, "stderr"), daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _append_output(self, text: str) -> None:
        self.output_buffer.append(text)
        self._output_chars += len(text)
        while self._output_chars > self.max_output_chars and self.output_buffer:
            excess = self._output_chars - self.max_output_chars
            first = self.output_buffer[0]
            if len(first) <= excess:
                removed = self.output_buffer.popleft()
                self._output_chars -= len(removed)
                self.output_discarded_chars += len(removed)
            else:
                self.output_buffer[0] = first[excess:]
                self._output_chars -= excess
                self.output_discarded_chars += excess
                break

    def _read_stream(self, stream, stream_name: str) -> None:
        """分块读取流，减少系统调用和锁竞争。"""
        if stream is None:
            return

        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    with self._lock:
                        self._append_output(text)
            final_text = decoder.decode(b"", final=True)
            if final_text:
                with self._lock:
                    self._append_output(final_text)
        except Exception as e:
            logger.debug("Error reading %s for process %s: %s", stream_name, self.id, e)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def get_output(self, limit: int = 12000) -> tuple[str, bool]:
        """获取当前进程已输出的合并文本。"""
        with self._lock:
            full_output = "".join(self.output_buffer)
            discarded = self.output_discarded_chars

        truncated = discarded > 0
        if discarded:
            full_output = f"...[前面 {discarded} 字符输出已丢弃，仅保留最近输出]\n" + full_output
        if len(full_output) > limit:
            return "..." + full_output[-(limit - 3):], True
        return full_output, truncated

    def send_input(self, text: str) -> None:
        """向进程的标准输入发送文本。"""
        if self.is_running() and self.process.stdin:
            try:
                self.process.stdin.write(text.encode("utf-8"))
                self.process.stdin.flush()
                logger.info("Sent input to process %s", self.id)
            except Exception as e:
                logger.error("Failed to send input to %s: %s", self.id, e)
                raise

    def is_running(self) -> bool:
        """检查进程是否仍在运行。"""
        return self.process.poll() is None

    def terminate(self) -> None:
        """终止进程。"""
        if self.is_running():
            logger.info("Terminating process %s", self.id)
            self.process.terminate()

    def cleanup(self) -> None:
        """释放资源，关闭所有相关的文件描述符。"""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except Exception:
            pass
        if self.is_running():
            self.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        else:
            self.process.wait()
        for thread in (self.stdout_thread, self.stderr_thread):
            try:
                thread.join(timeout=0.5)
            except RuntimeError:
                pass


class ProcessManager:
    """全局进程管理。"""

    def __init__(self, max_history: int = 50):
        self._processes: dict[str, RunningProcess] = {}
        self.max_history = max_history

    def reap_finished(self) -> int:
        """清理已结束的进程，释放资源并移除记录。"""
        finished_ids = [p_id for p_id, p in self._processes.items() if not p.is_running()]
        for p_id in finished_ids:
            proc = self._processes.pop(p_id, None)
            if proc:
                proc.cleanup()
        return len(finished_ids)

    def _cleanup_old_processes(self) -> None:
        """清理已结束的进程，防止内存泄漏。"""
        self.reap_finished()
        if len(self._processes) >= self.max_history:
            for p_id in list(self._processes)[: len(self._processes) - self.max_history + 1]:
                proc = self._processes.pop(p_id, None)
                if proc:
                    proc.cleanup()

    def start_process(self, command: list[str], cwd: Path, shell_name: str, session_id: str | None = None) -> RunningProcess:
        self._cleanup_old_processes()
        proc = RunningProcess(command, cwd, shell_name, session_id=session_id)
        self._processes[proc.id] = proc
        return proc

    def get_process(self, process_id: str) -> Optional[RunningProcess]:
        return self._processes.get(process_id)

    def terminate_process(self, process_id: str) -> None:
        proc = self._processes.pop(process_id, None)
        if proc:
            proc.cleanup()

    def terminate_session(self, session_id: str) -> int:
        """终止指定 session 启动且仍在运行的后台进程。"""
        count = 0
        for proc_id, proc in list(self._processes.items()):
            if proc.session_id != session_id or not proc.is_running():
                continue
            logger.info("Terminating process %s for session %s", getattr(proc, "id", "unknown"), session_id)
            proc.cleanup()
            self._processes.pop(proc_id, None)
            count += 1
        return count
