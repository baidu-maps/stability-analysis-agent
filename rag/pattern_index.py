#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pattern index for vector retrieval (ChromaDB).
"""

import logging
import os
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

# 在导入 chromadb 前禁用遥测，减少对遥测模块的依赖。
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["CHROMA_TELEMETRY"] = "False"

logger = logging.getLogger(__name__)

VECTOR_DB_AVAILABLE = False
_np: Any = None
_chromadb: Any = None
_Settings: Any = None

try:
    import numpy as _np
    import chromadb as _chromadb
    from chromadb.config import Settings as _Settings

    VECTOR_DB_AVAILABLE = True
    warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")
except Exception as e:
    logger.warning(
        "向量数据库依赖导入失败（%s）。相似案例检索将不可用；"
        "可安装: pip install 'stability-analysis-agent[rag]'",
        e,
    )


class PatternIndex:
    def __init__(self, db_path: str = "./vector_db"):
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.embedding_model = None
        self.client = None
        self.collection = None
        if not VECTOR_DB_AVAILABLE or _chromadb is None or _Settings is None:
            logger.warning("向量数据库依赖不可用，将使用模拟模式（简单哈希嵌入）")
            return
        try:
            # 控制是否启用 SentenceTransformer 预训练模型：
            # - 默认关闭（不访问 HuggingFace，只使用简单哈希嵌入，完全本地、无网络依赖）
            # - 如需启用，可设置环境变量 AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL=1
            enable_sentence_model = os.environ.get("AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL", "").lower() in (
                "1",
                "true",
                "yes",
            )

            if enable_sentence_model:
                if "HF_ENDPOINT" not in os.environ:
                    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
                os.environ.setdefault("HF_HUB_DISABLE_EXPERIMENTAL_WARNING", "1")
                os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
                try:
                    from sentence_transformers import SentenceTransformer

                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")
                        warnings.filterwarnings("ignore", message=".*Connection.*")
                        self.embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
                    logger.info("嵌入模型初始化成功（SentenceTransformer，all-MiniLM-L6-v2）")
                except Exception as model_error:
                    error_msg = str(model_error)
                    if "Connection" in error_msg or "timeout" in error_msg.lower() or "reset" in error_msg.lower():
                        logger.info("加载 SentenceTransformer 时发生网络连接问题，将退回到简单哈希嵌入模式")
                    else:
                        logger.warning(f"预训练向量模型加载失败，将退回到简单哈希嵌入: {model_error}")
                    self.embedding_model = None
            else:
                logger.info(
                    "已禁用 SentenceTransformer 预训练模型，默认使用简单哈希嵌入 "
                    "(可通过环境变量 AI_STABILITY_ANALYZER_ENABLE_SENTENCE_MODEL=1 启用)"
                )

            # 显式使用纯 Python 的 SegmentAPI，而不是默认的 RustBindingsAPI，
            # 以避免在未安装 Rust 扩展或打包环境下导入 chromadb.api.rust 失败。
            self.client = _chromadb.PersistentClient(
                path=str(self.db_path),
                settings=_Settings(
                    anonymized_telemetry=False,
                    chroma_api_impl="chromadb.api.segment.SegmentAPI",
                ),
            )
            self.collection = self.client.get_or_create_collection(
                name="crash_pattern_index",
                metadata={"description": "Crash pattern index for retrieval"},
            )
        except Exception as e:
            msg = str(e)
            # 在 CLI / 打包环境中，chromadb 可能无法加载可选的 Rust 后端（chromadb.api.rust），
            # 此时仅禁用向量数据库能力，不影响基础崩溃解析功能。
            if isinstance(e, ModuleNotFoundError):
                logger.warning(
                    f"向量数据库依赖缺失（{e.name}），将禁用相似案例检索功能；"
                    "基础崩溃解析与单次 AI 分析不受影响。"
                )
            elif "chromadb.api.rust" in msg or "api.rust" in msg:
                logger.warning(
                    "向量数据库后端（chromadb.api.rust）不可用，将禁用相似案例检索功能；"
                    "基础崩溃解析与单次 AI 分析不受影响。"
                )
            else:
                logger.error(f"向量数据库初始化失败: {e}")
            self.embedding_model = None
            self.client = None
            self.collection = None

    def _get_embedding(self, text: str) -> List[float]:
        if not self.embedding_model:
            return self._simple_hash_embedding(text)
        try:
            embedding = self.embedding_model.encode(text)
            return embedding.tolist()
        except Exception as e:
            logger.error(f"获取嵌入向量失败: {e}")
            return self._simple_hash_embedding(text)

    def _simple_hash_embedding(self, text: str) -> List[float]:
        if _np is not None:
            _np.random.seed(abs(hash(text)) % (2**32))
            vector = _np.random.rand(384)
            vector = vector / _np.linalg.norm(vector)
            return vector.tolist()
        # 无 numpy 时的纯 Python 回退（384 维）
        import hashlib
        import struct

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        while len(floats) < 384:
            for i in range(0, len(digest) - 3, 4):
                floats.append(struct.unpack("!f", digest[i : i + 4])[0])
                if len(floats) >= 384:
                    break
            digest = hashlib.sha256(digest).digest()
        norm = sum(x * x for x in floats) ** 0.5 or 1.0
        return [x / norm for x in floats]

    def add_pattern(self, pattern_id: str, pattern_summary: str, crash_signature: str, metadata: Dict[str, Any]) -> bool:
        if not self.collection:
            logger.error("模式索引集合未初始化")
            return False
        try:
            text = f"{pattern_summary} {crash_signature}".strip()
            embedding = self._get_embedding(text)
            meta = dict(metadata or {})
            meta["pattern_id"] = pattern_id
            self.collection.add(
                embeddings=[embedding],
                documents=[text],
                metadatas=[meta],
                ids=[pattern_id],
            )
            return True
        except Exception as e:
            logger.error(f"添加模式索引失败: {e}")
            return False

    def query(self, query_text: str, n_results: int = 5, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if not self.collection:
            logger.error("模式索引集合未初始化")
            return []
        try:
            embedding = self._get_embedding(query_text)
            results = self.collection.query(
                query_embeddings=[embedding],
                n_results=n_results,
                where=where,
            )
            hits: List[Dict[str, Any]] = []
            if results.get("ids") and results.get("ids")[0]:
                for i, pid in enumerate(results["ids"][0]):
                    hits.append(
                        {
                            "pattern_id": pid,
                            "distance": results["distances"][0][i] if results.get("distances") else 0.0,
                            "metadata": results["metadatas"][0][i] if results.get("metadatas") else {},
                        }
                    )
            return hits
        except Exception as e:
            logger.error(f"搜索模式索引失败: {e}")
            return []

    def count(self) -> int:
        if not self.collection:
            return 0
        try:
            return int(self.collection.count())
        except Exception:
            return 0

    def clear_all(self) -> bool:
        """清空向量索引（删除集合内全部文档）。"""
        if not self.collection:
            return False
        try:
            result = self.collection.get()
            ids = result.get("ids") or []
            if ids:
                self.collection.delete(ids=ids)
            return True
        except Exception as e:
            logger.error(f"清空向量索引失败: {e}")
            return False
