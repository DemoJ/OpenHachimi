"""后台进程管理器。

提供异步启动、持续读取输出、以及向交互式进程发送输入的能力。
"""

import logging
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class RunningProcess:
    """包装正在后台运行的子进程。"""

    def __init__(self, command: list[str], cwd: Path, shell_name: str):
        self.id = str(uuid.uuid4())
        self.command = command
        self.cwd = cwd.as_posix()
        self.shell_name = shell_name
        
        logger.info(f"Starting process {self.id}: {command} in {cwd}")
        
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered where possible
            encoding="utf-8",
            errors="replace",
        )
        
        self.output_buffer: list[str] = []
        self._lock = threading.Lock()
        
        # 启动守护线程读取 stdout 和 stderr
        self.stdout_thread = threading.Thread(
            target=self._read_stream, 
            args=(self.process.stdout, "stdout"), 
            daemon=True
        )
        self.stderr_thread = threading.Thread(
            target=self._read_stream, 
            args=(self.process.stderr, "stderr"), 
            daemon=True
        )
        
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stream(self, stream, stream_name: str) -> None:
        """逐字符读取流，以确保即使没有换行符的交互式提示也能被捕获。"""
        if stream is None:
            return
            
        try:
            while True:
                # 逐字符读取对于交互式提示非常重要（如 "Confirm? [y/N]: "）
                char = stream.read(1)
                if not char:
                    break
                with self._lock:
                    self.output_buffer.append(char)
        except Exception as e:
            logger.debug("Error reading %s for process %s: %s", stream_name, self.id, e)

    def get_output(self, limit: int = 12000) -> tuple[str, bool]:
        """获取当前进程已输出的合并文本。"""
        with self._lock:
            # Join all characters
            full_output = "".join(self.output_buffer)
            
        # 截断以避免超出大模型上下文
        if len(full_output) > limit:
            # 取最后一部分输出，因为后台长任务往往最后的输出更重要
            return "..." + full_output[-(limit - 3):], True
        return full_output, False
            
    def send_input(self, text: str) -> None:
        """向进程的标准输入发送文本。"""
        if self.is_running() and self.process.stdin:
            try:
                self.process.stdin.write(text)
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


class ProcessManager:
    """全局进程管理。"""

    def __init__(self):
        self._processes: dict[str, RunningProcess] = {}
        
    def start_process(self, command: list[str], cwd: Path, shell_name: str) -> RunningProcess:
        proc = RunningProcess(command, cwd, shell_name)
        self._processes[proc.id] = proc
        return proc
        
    def get_process(self, process_id: str) -> Optional[RunningProcess]:
        return self._processes.get(process_id)
        
    def terminate_process(self, process_id: str) -> None:
        proc = self.get_process(process_id)
        if proc:
            proc.terminate()


# 单例实例，跨请求共享进程状态
process_manager = ProcessManager()
