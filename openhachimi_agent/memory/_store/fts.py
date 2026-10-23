"""FTS5 全文索引:中文二元分词、索引文本与查询表达式构造、旧索引重建。

SQLite FTS5 默认的 unicode61 分词器把连续的汉字整体当成一个词,"请用中文回答我"
只能命中一模一样的整句,检索基本失效。这里在 Python 侧把每段连续汉字切成重叠的
二元组(用中 / 中文 / 文回 …),索引和查询两侧用同一套切法,不依赖 SQLite 版本
或 trigram 分词器。
"""

from __future__ import annotations

import sqlite3

from openhachimi_agent.memory._store.utils import _load_json_array

# 单条查询最多使用的检索词数。汉字按二元组展开后词数会变多,上限比旧实现的 12 高。
MAX_QUERY_TERMS = 32
_MAX_TERM_CHARS = 32


def _is_cjk(char: str) -> bool:
    return "一" <= char <= "鿿"


def _runs(text: str) -> list[tuple[bool, str]]:
    """把文本切成连续的同类片段:(是否汉字, 片段)。标点、空白等非字母数字字符作分隔。"""
    runs: list[tuple[bool, str]] = []
    current: list[str] = []
    current_cjk = False
    for char in text:
        if not char.isalnum():
            if current:
                runs.append((current_cjk, "".join(current)))
                current = []
            continue
        cjk = _is_cjk(char)
        if current and cjk != current_cjk:
            runs.append((current_cjk, "".join(current)))
            current = []
        current_cjk = cjk
        current.append(char)
    if current:
        runs.append((current_cjk, "".join(current)))
    return runs


def _cjk_bigrams(run: str) -> list[str]:
    if len(run) < 2:
        return [run]
    return [run[index:index + 2] for index in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """切成检索词:非汉字片段原样保留(截断到 32 字符),汉字片段切成二元组。保持顺序并去重。"""
    terms: list[str] = []
    for is_cjk, run in _runs(text or ""):
        if is_cjk:
            terms.extend(_cjk_bigrams(run))
        else:
            terms.append(run[:_MAX_TERM_CHARS])
    return list(dict.fromkeys(terms))


def index_text(*parts: str | None) -> str:
    """生成写入 FTS 列的文本:原文后追加分词结果,保证中文二元组和中英混排里的英文词都能命中。"""
    text = " ".join(part for part in parts if part)
    tokens = tokenize(text)
    return f"{text} {' '.join(tokens)}" if tokens else text


def query_terms(query: str) -> list[str]:
    return tokenize(query)[:MAX_QUERY_TERMS]


def match_expression(terms: list[str]) -> str:
    """把检索词拼成 FTS5 MATCH 表达式。每个词加双引号,避免 AND / OR / NOT 被当成运算符。"""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def rebuild_fts_indexes(conn: sqlite3.Connection) -> None:
    """按当前分词规则重建三张 FTS 表(只收录 active 行)。用于把旧库的索引升级到新分词。"""
    conn.execute("DELETE FROM memory_atoms_fts")
    for row in conn.execute(
        "SELECT id, content, search_text, keywords_json FROM memory_atoms WHERE status = 'active'"
    ).fetchall():
        keywords = " ".join(_load_json_array(row["keywords_json"]))
        conn.execute(
            "INSERT INTO memory_atoms_fts(id, content, search_text, keywords) VALUES(?,?,?,?)",
            (row["id"], index_text(row["content"]), index_text(row["search_text"]), index_text(keywords)),
        )
    conn.execute("DELETE FROM memory_blocks_fts")
    for row in conn.execute(
        "SELECT id, title, summary, details, search_text, keywords_json FROM memory_blocks WHERE status = 'active'"
    ).fetchall():
        keywords = " ".join(_load_json_array(row["keywords_json"]))
        conn.execute(
            "INSERT INTO memory_blocks_fts(id, title, summary, details, search_text, keywords) VALUES(?,?,?,?,?,?)",
            (
                row["id"],
                index_text(row["title"]),
                index_text(row["summary"]),
                index_text(row["details"]),
                index_text(row["search_text"]),
                index_text(keywords),
            ),
        )
    conn.execute("DELETE FROM memory_profiles_fts")
    for row in conn.execute(
        "SELECT id, title, summary FROM memory_profiles WHERE status = 'active'"
    ).fetchall():
        conn.execute(
            "INSERT INTO memory_profiles_fts(id, title, summary) VALUES(?,?,?)",
            (row["id"], index_text(row["title"]), index_text(row["summary"])),
        )
