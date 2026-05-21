"""长期记忆冲突处理。"""

from __future__ import annotations

from openhachimi_agent.memory.models import MemoryAtom


def normalized_memory_key(atom: MemoryAtom) -> tuple[str, str, str, str]:
    return (atom.memory_type, atom.subject.lower(), atom.predicate.lower(), (atom.object or atom.content).strip().lower())
