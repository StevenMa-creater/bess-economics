"""执行 sql/ 目录下的查询库，打印结果并保存为 CSV。

查询文件的格式：每条查询以一行 `-- @name: 名字` 开头，后面是中文注释（业务问题 / 知识点 / 结果解读），
再后面是 SQL。一个块里可以有多条语句（比如先 CREATE TABLE 再 SELECT），返回最后一条的结果。

    python run.py sql run --level 1        # 只跑 01_basics.sql
    python run.py sql run --level all      # 三级全部
    python run.py sql query "SELECT COUNT(*) FROM fact_price"
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

LEVEL_FILES = {1: "01_basics.sql", 2: "02_intermediate.sql", 3: "03_advanced.sql"}


@dataclass
class Query:
    name: str
    sql: str
    comments: list[str] = field(default_factory=list)

    @property
    def question(self) -> str:
        """注释里「业务问题」那一行，打印时当标题用。"""
        for c in self.comments:
            if c.startswith("业务问题"):
                return c.split("：", 1)[-1].strip()
        return ""


def parse(path: Path) -> list[Query]:
    """把一个 .sql 文件拆成若干条带名字的查询。"""
    text = Path(path).read_text(encoding="utf-8")
    blocks = re.split(r"^--\s*@name:\s*", text, flags=re.M)[1:]
    out = []
    for b in blocks:
        name, _, body = b.partition("\n")
        comments = [ln.strip()[2:].strip() for ln in body.splitlines() if ln.strip().startswith("--")]
        out.append(Query(name.strip(), body.strip(), comments))
    return out


def split_statements(sql: str) -> list[str]:
    """按分号拆成多条语句（用 sqlite3.complete_statement 判断一条语句是否完整，字符串里的分号不会误拆）。"""
    stmts, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                stmts.append(buf.strip())
            buf = ""
    if buf.strip() and re.sub(r"--.*", "", buf).strip():
        stmts.append(buf.strip())
    return stmts


def execute(conn: sqlite3.Connection, sql: str) -> pd.DataFrame | None:
    """执行一条或多条语句，返回最后一条有结果集的语句的结果（DataFrame）。"""
    result = None
    for stmt in split_statements(sql):
        cur = conn.execute(stmt)
        if cur.description:  # SELECT / EXPLAIN 才有 description
            result = pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
    conn.commit()
    return result


def _show(df: pd.DataFrame, max_rows: int) -> str:
    with pd.option_context("display.max_columns", 20, "display.width", 160, "display.float_format", "{:,.2f}".format):
        s = df.head(max_rows).to_string(index=False)
    if len(df) > max_rows:
        s += f"\n…（共 {len(df):,} 行，完整结果见 CSV）"
    return s


def run_file(conn: sqlite3.Connection, path: Path, out_dir: Path | None, max_rows: int = 12, log=print) -> dict:
    """依次执行文件里的每条查询；结果保存到 out_dir/<name>.csv（utf-8-sig 编码，Excel 打开不乱码）。"""
    results = {}
    for q in parse(path):
        t0 = time.perf_counter()
        df = execute(conn, q.sql)
        ms = (time.perf_counter() - t0) * 1000
        results[q.name] = df
        log(f"\n=== {q.name} · {q.question}  （{ms:,.1f} ms）")
        if df is None:
            log("（没有返回结果）")
            continue
        log(_show(df, max_rows))
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            df.to_csv(out_dir / f"{q.name}.csv", index=False, encoding="utf-8-sig")
    return results


def run_levels(conn: sqlite3.Connection, sql_dir: Path, levels, out_dir: Path | None, max_rows: int = 12, log=print) -> dict:
    results = {}
    for lv in levels:
        log(f"\n################ L{lv} · {LEVEL_FILES[lv]} ################")
        results.update(run_file(conn, Path(sql_dir) / LEVEL_FILES[lv], out_dir, max_rows, log))
    return results
