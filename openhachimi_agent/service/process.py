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
        
        logger.info("Starting process %s: %s in %s", self.id, command, cwd)
        
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            bufsize=0,  # Unbuffered binary mode
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
        """分块读取流，减少系统调用和锁竞争。使用增量解码器处理跨块字符，兼顾交互提示符的实时性。"""
        if stream is None:
            return
            
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
            
        try:
            while True:
                # unbuffered 模式下 read 会在有数据时立即返回
                chunk = stream.read(4096)
                if not chunk:
                    break
                
                text = decoder.decode(chunk)
                if text:
                    with self._lock:
                        self.output_buffer.append(text)
                        
            final_text = decoder.decode(b"", final=True)
            if final_text:
                with self._lock:
                    self.output_buffer.append(final_text)
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


class ProcessManager:
    """全局进程管理。"""

    def __init__(self, max_history: int = 50):
        self._processes: dict[str, RunningProcess] = {}
        self.max_history = max_history

    def _cleanup_old_processes(self) -> None:
        """清理已结束的进程，防止内存泄漏。"""
        finished_ids = [p_id for p_id, p in self._processes.items() if not p.is_running()]
        
        if len(self._processes) >= self.max_history:
            # 清理超出限制数量的已结束进程
            excess = len(self._processes) - self.max_history + 1
            for p_id in finished_ids[:excess]:
                proc = self._processes.pop(p_id, None)
                if proc:
                    proc.cleanup()
        
    def start_process(self, command: list[str], cwd: Path, shell_name: str) -> RunningProcess:
        self._cleanup_old_processes()
        proc = RunningProcess(command, cwd, shell_name)
        self._processes[proc.id] = proc
        return proc
        
    def get_process(self, process_id: str) -> Optional[RunningProcess]:
        return self._processes.get(process_id)
        
    def terminate_process(self, process_id: str) -> None:
        proc = self.get_process(process_id)
        if proc:
            proc.terminate()

