"""zvec.py — 语义索引发布（§15）：generation + 原子指针 + reader 锁协议 + 三方 hash 验证。

目录结构：
  .repo-memory-kit/zvec/
    generations/<uuid>/          # _data.db / .metadata.json / .doc-count / .lock
    current.json                 # {"generation": "<uuid>", "docs": N}
    rebuild.lock                 # 构建方进程锁

子进程模式（--rebuild-gen）只读语料快照构建索引；父进程始终用
``python -m installer.zvec`` 启动它，保持包导入语义，避免 installer/platform
遮蔽标准库 platform。语料提取复用 kit 侧 memory-recall 的 build_docs
（单一语料口径，防漂移）。
zvec rebuild 不取 install.lock（派生数据，有自己的 rebuild.lock，§11.3）。
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ZVEC_REL = ".repo-memory-kit/zvec"


# ══════════════════════════ 子进程构建（只读快照；stdlib only） ══════════════════════════

_SCHEMA_FIELDS = (
    ("path", "STRING"), ("title", "STRING"), ("type", "STRING"),
    ("status", "STRING"), ("module", "STRING"), ("verified", "STRING"),
    ("meta", "STRING"), ("content", "STRING"),
)


def _rebuild_gen_main(gen_dir: Path, snapshot: Path) -> int:
    """子进程：只读快照构建 _data.db；写 .build-metadata.json（正式
    .metadata.json 由父进程验证后首次创建）。"""
    import zvec
    from zvec import (CollectionOption, DataType, Doc, FieldSchema,
                      FtsIndexParam, CollectionSchema)

    data = snapshot.read_bytes()
    corpus_hash = hashlib.sha256(data).hexdigest()
    docs = [json.loads(line) for line in data.decode("utf-8").split("\n") if line]
    if not docs:
        print("✗ 语料为空，不发布 generation", file=sys.stderr)
        return 1

    jieba = FtsIndexParam(tokenizer_name="jieba")
    schema = CollectionSchema(
        name="recall",
        fields=[
            FieldSchema(name, DataType.STRING, nullable=True,
                        index_param=jieba if name in ("meta", "content") else None)
            for name, _ in _SCHEMA_FIELDS
        ],
    )
    coll = zvec.create_and_open(str(gen_dir), schema,
                                CollectionOption(read_only=False, enable_mmap=True))
    # zvec 文档 ID：完整 SHA-256，不截断（§15.3 防碰撞）
    payload = [
        Doc(id=hashlib.sha256(d["rel"].encode("utf-8")).hexdigest(), fields={
            "path": d["rel"], "title": d["title"], "type": d["type"],
            "status": d["status"], "module": d["module"], "verified": d["verified"],
            "meta": d["meta"], "content": d["content"][:20000],
        })
        for d in docs
    ]
    statuses = coll.upsert(payload)
    bad = [str(s) for s in statuses if s and not getattr(s, "ok", lambda: True)()]
    if bad:
        print(f"✗ {len(bad)} 条写入异常——索引不发布", file=sys.stderr)
        return 1
    # zvec 0.7.0 已知限制：小数据集的 flush/close 有 stderr 噪音，数据在 mmap 中
    for _attr in ("flush", "close"):
        try:
            getattr(coll, _attr)()
        except Exception:
            pass
    del coll
    gc.collect()          # 强制 GC 触发 C++ 析构
    time.sleep(0.3)       # 等 LSM 后台线程
    (gen_dir / ".build-metadata.json").write_text(
        json.dumps({"corpus_hash": corpus_hash}), encoding="utf-8")
    return 0


# ══════════════════════════ 父进程：语料与发布 ══════════════════════════

def _load_memory_recall():
    """载入 kit 侧 memory-recall（无 .py 后缀，SourceFileLoader）——
    build_docs 单一语料口径：发布端与查询端必须一致。"""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    from .registry import KIT_DIR
    path = KIT_DIR / "memory-recall"
    loader = SourceFileLoader("kit_memory_recall_zvec", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def compute_corpus_hash(docs: list[dict]) -> str:
    """与快照构造完全一致的语料 hash（jsonl 逐行 sort_keys 序列化后 \\n 连接）。"""
    lines = [json.dumps(d, ensure_ascii=False, sort_keys=True) for d in docs]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def rebuild(repo: Path) -> None:
    """§15.2 重建流程（rebuild.lock 进程锁；generation 原子发布）。"""
    from . import platform as _plat
    from .registry import (secure_mkdir, secure_open, secure_replace,
                           secure_unlink, fsync_dir, write_all)

    repo = repo.resolve()
    zvec_dir = repo / ZVEC_REL
    gen_name = str(uuid.uuid4())
    gen_dir = zvec_dir / "generations" / gen_name

    # 0. 基础设施目录（幂等；首次重建必须创建，否则 rebuild.lock 落盘即 ENOENT——v12）。
    #    只建到 generations/ 为止：zvec.create_and_open 要求目标路径**不存在**，
    #    generation 叶子目录由子进程构建时创建（§15.2 的 ENOENT 修复目标是
    #    rebuild.lock 的父目录 zvec/，不是叶子目录本身）。
    secure_mkdir(repo, ZVEC_REL)
    secure_mkdir(repo, f"{ZVEC_REL}/generations")

    # 1. 进程锁（secure_open：O_NOFOLLOW、无 O_TRUNC——
    #    open(path, "w") 会跟随符号链接并截断外部文件）
    lock_fd = secure_open(repo, f"{ZVEC_REL}/rebuild.lock",
                          os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        _plat.lock_exclusive_nb(lock_fd)
    except BlockingIOError:
        os.close(lock_fd)
        sys.exit("另一个 rebuild 正在运行")
    try:
        # 2. 父进程固定完整语料快照（JSONL，含所有索引字段）
        recall = _load_memory_recall()
        docs = recall.build_docs(repo)
        if not docs:
            sys.exit("✗ docs/ 下没有可索引的 Markdown 文档")
        snapshot_hash = compute_corpus_hash(docs)

        snapshot_rel = f"{ZVEC_REL}/snapshot-{gen_name}.jsonl"
        snapshot_fd = secure_open(repo, snapshot_rel,
                                  os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            write_all(snapshot_fd, "\n".join(
                json.dumps(d, ensure_ascii=False, sort_keys=True)
                for d in docs).encode())
            os.fsync(snapshot_fd)
        finally:
            os.close(snapshot_fd)

        # 3. 子进程构建（只读快照，不读源 docs/）
        #    超时必须回收未发布的 generation——否则留下无 .metadata.json 的
        #    目录，cleanup_old_generations（只回收带 .metadata 的）永不收集
        child_env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent.parent)
        inherited_pythonpath = child_env.get("PYTHONPATH")
        child_env["PYTHONPATH"] = os.pathsep.join(
            [package_root] + ([inherited_pythonpath] if inherited_pythonpath else [])
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "installer.zvec", "--rebuild-gen",
                 str(gen_dir), str(repo / snapshot_rel)],
                check=False, timeout=120, env=child_env,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(gen_dir, ignore_errors=True)
            sys.exit("✗ 子进程构建超时（120s），已回收未发布的 generation——"
                     "语料过大或磁盘过慢，可重试")
        finally:
            secure_unlink(repo, snapshot_rel)
        if proc.returncode != 0:
            shutil.rmtree(gen_dir, ignore_errors=True)
            sys.exit(f"✗ 子进程构建失败（exit={proc.returncode}）")

        # 4-5. 验证：三方 hash + 路径集合。任何异常/失败 → 未发布的 generation
        # 一律回收（未过 .metadata.json 发布点，对读者不可见，可安全删除）。
        try:
            build_meta = json.loads((gen_dir / ".build-metadata.json").read_text())
            if build_meta["corpus_hash"] != snapshot_hash:
                shutil.rmtree(gen_dir, ignore_errors=True)
                sys.exit("子进程语料 hash 与父进程不一致")

            current_docs = recall.build_docs(repo)
            current_hash = compute_corpus_hash(current_docs)
            if snapshot_hash != current_hash:
                shutil.rmtree(gen_dir, ignore_errors=True)
                sys.exit("语料在构建期间变化")

            # 5. 验证索引路径集合（用 iter_docs，不用 query）
            import zvec
            coll = zvec.open(str(gen_dir))
            indexed_paths = set()
            for doc in coll.iter_docs(output_fields=["path"], include_vector=False):
                indexed_paths.add(doc.fields.get("path"))
            coll.close()
            del coll
            gc.collect()

            expected_paths = {d["rel"] for d in docs}
            if indexed_paths != expected_paths:
                shutil.rmtree(gen_dir, ignore_errors=True)
                sys.exit("索引路径集不匹配")
        except BaseException:
            shutil.rmtree(gen_dir, ignore_errors=True)
            raise

        # 6. 父进程首次创建正式元数据 + 协议锁（全部 secure_open + fsync）
        gen_rel = f"{ZVEC_REL}/generations/{gen_name}"
        meta = json.dumps({
            "generation": gen_name,
            "docs": len(docs),
            "corpus_hash": snapshot_hash,
            "paths_sha": hashlib.sha256(
                "\n".join(sorted(expected_paths)).encode()).hexdigest(),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).encode()
        for rel, content, mode in [
            (f"{gen_rel}/.metadata.json", meta, 0o600),
            (f"{gen_rel}/.doc-count", str(len(docs)).encode(), 0o644),
            (f"{gen_rel}/.lock", b"", 0o600),
        ]:
            fd = secure_open(repo, rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                write_all(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
        secure_unlink(repo, f"{gen_rel}/.build-metadata.json")   # 校验完成，删除构建中间物
        fsync_dir(gen_dir)               # generation 落盘完成后才能发布

        # 7. 原子切换 pointer（secure_open：唯一 tmp + O_EXCL|O_NOFOLLOW + fsync）
        pointer = {"generation": gen_name, "docs": len(docs)}
        tmp_rel = f"{ZVEC_REL}/current.json.{uuid.uuid4()}.tmp"
        fd = secure_open(repo, tmp_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            write_all(fd, json.dumps(pointer).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        secure_replace(repo, tmp_rel, f"{ZVEC_REL}/current.json")

        # 8. 延迟回收（§15.4：锁协议是正确性保证，min_age 只是纵深防御）
        cleanup_old_generations(zvec_dir, keep=2, min_age_seconds=600)
        print(f"✓ 语义索引已发布：{len(docs)} 篇文档 → generations/{gen_name}")
    finally:
        os.close(lock_fd)


def read_current(repo: Path) -> Path:
    """读路径：验证 pointer 安全后返回 generation 目录（§15.4 reader 的第一步）。"""
    from .registry import SecurityError, validate_tx_id
    zvec_dir = repo / ZVEC_REL
    pointer = json.loads((zvec_dir / "current.json").read_text())
    gen_name = pointer.get("generation", "")
    validate_tx_id(gen_name)   # 复用 UUID v4 校验
    gen_dir = (zvec_dir / "generations" / gen_name).resolve()
    generations_root = (zvec_dir / "generations").resolve()
    if os.path.commonpath([str(gen_dir), str(generations_root)]) != str(generations_root):
        raise SecurityError("Generation path escaped")
    return gen_dir


def open_current_generation(repo: Path, retries: int = 3):
    """reader 必须持共享锁打开 generation，否则可能被并发清理回收。
    返回 (gen_dir, lock_fd)；查询期间保持 fd 打开，结束后 close（即释放）。
    "数据在"的判定用 .metadata.json（发布标记，与 .lock 同批创建）——
    zvec 0.7.0 的 RocksDB 布局没有设计稿所称的 _data.db 单文件。"""
    from . import platform as _plat
    for _ in range(retries):
        gen_dir = read_current(repo)                 # pointer 校验（UUID + 逃逸检查）
        lock_file = gen_dir / ".lock"
        try:
            fd = os.open(lock_file,
                         os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
        except FileNotFoundError:
            continue                                 # 正被回收 → 重读 pointer
        try:
            _plat.lock_shared_nb(fd)
        except BlockingIOError:
            os.close(fd)
            continue                                 # 清理方持 EX → 重读 pointer
        if (gen_dir / ".metadata.json").exists():
            return gen_dir, fd                       # 锁有效且数据在 → 安全查询
        os.close(fd)                                 # 锁到手但数据已删 → 重来
    raise RuntimeError("zvec 索引正在重建，稍后重试")


def cleanup_old_generations(zvec_dir: Path, keep: int, min_age_seconds: int) -> None:
    """删除前必须对该 generation 的 .lock 取排他锁：取不到 = 有 reader 在用 → 跳过。
    min_age（第二道防线）之后仍会再次尝试，但正确性不依赖它。"""
    from . import platform as _plat
    from .registry import SecurityError, validate_tx_id
    pointer = json.loads((zvec_dir / "current.json").read_text())
    current_name = pointer.get("generation", "")
    entries = []
    for g in (zvec_dir / "generations").iterdir():
        try:
            validate_tx_id(g.name)
        except SecurityError:
            continue
        meta_path = g / ".metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            entries.append((meta.get("built_at", ""), g))

    entries.sort(key=lambda x: x[0], reverse=True)
    cutoff = time.time() - min_age_seconds
    for built_at, g in entries[keep:]:
        if g.name == current_name:
            continue
        if g.stat().st_mtime > cutoff:
            continue
        lock_file = g / ".lock"
        try:
            fd = os.open(lock_file,
                         os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
        except OSError:
            continue
        try:
            _plat.lock_exclusive_nb(fd)
        except BlockingIOError:
            os.close(fd)
            continue
        try:
            shutil.rmtree(g)
        finally:
            os.close(fd)


# ══════════════════════════ CLI ══════════════════════════

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--rebuild-gen":
        if len(args) != 3:
            print("用法（内部）: zvec.py --rebuild-gen <gen_dir> <snapshot>", file=sys.stderr)
            return 2
        return _rebuild_gen_main(Path(args[1]), Path(args[2]))
    if len(args) == 2 and args[0] == "--check":
        repo = Path(args[1]).expanduser().resolve()
        gen_dir, lock_fd = open_current_generation(repo)
        try:
            print(f"✓ current generation: {gen_dir.name}")
            return 0
        finally:
            os.close(lock_fd)
    if len(args) == 1:
        rebuild(Path(args[0]).expanduser().resolve())
        return 0
    print("用法: python3 -m installer.zvec <repo> [--check <repo>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
