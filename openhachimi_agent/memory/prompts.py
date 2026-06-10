"""长期记忆相关提示词。"""

from openhachimi_agent.content.prompts import load_system_prompt


MEMORY_EXTRACTION_PROMPT = load_system_prompt("memory/base")
MEMORY_RERANK_PROMPT = load_system_prompt("memory/rerank")
