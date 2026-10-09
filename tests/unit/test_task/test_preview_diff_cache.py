"""
测试 ToolBackupManager.preview_rollback_diff 的短时结果缓存

场景：用户反复点击同一回滚链接查看预览时，TTL 内命中缓存，
跳过 add -A 全量扫描（大仓库并发构建时可耗时分钟级）。
"""
from unittest.mock import patch

import pytest

from peng.service import tool_backup
from peng.service.tool_backup import ToolBackupManager


@pytest.fixture(autouse=True)
def clean_cache():
    """每个用例前后清空缓存，避免用例间污染"""
    tool_backup._preview_diff_cache.clear()
    yield
    tool_backup._preview_diff_cache.clear()


def _meta(workdir="/tmp/wd", commit="abc123"):
    return {
        "type": "git",
        "task_id": 999002,
        "tool_name": "iteration_start",
        "workdir": workdir,
        "nest_name": "local",
        "commit_hash": commit,
    }


class TestPreviewDiffCache:
    """preview_rollback_diff 缓存行为"""

    def test_remote_batch_result_cached(self):
        """远程批量结果应写入缓存，TTL内二次调用直接命中"""
        sentinel = {"success": True, "diff": "D", "changed_files": ["f"], "truncated": False, "message": "ok"}
        calls = []
        with patch.object(
            ToolBackupManager, "_preview_rollback_diff_remote_batch",
            side_effect=lambda *a, **kw: (calls.append(1) or sentinel),
        ):
            meta = _meta()
            meta["nest_name"] = "epyc"
            r1 = ToolBackupManager.preview_rollback_diff(meta)
            r2 = ToolBackupManager.preview_rollback_diff(meta)
        assert r1 is r2 is sentinel
        assert len(calls) == 1  # 第二次命中缓存，未真正执行

    def test_cache_expired_recomputes(self):
        """TTL过期后重新计算"""
        sentinel = {"success": True, "diff": "D", "changed_files": [], "truncated": False, "message": "ok"}
        calls = []
        meta = _meta()
        meta["nest_name"] = "epyc"
        with patch.object(
            ToolBackupManager, "_preview_rollback_diff_remote_batch",
            side_effect=lambda *a, **kw: (calls.append(1) or sentinel),
        ):
            ToolBackupManager.preview_rollback_diff(meta)
            # 模拟过期：回拨缓存时间戳
            key = f"{meta['nest_name']}|git|{meta['workdir']}|{meta['commit_hash']}"
            tool_backup._preview_diff_cache[key]["ts"] -= tool_backup._PREVIEW_DIFF_CACHE_TTL + 1
            ToolBackupManager.preview_rollback_diff(meta)
        assert len(calls) == 2

    def test_different_key_not_cached_hit(self):
        """不同 commit/workdir 的key不同，不互相命中"""
        r_a = {"success": True, "diff": "A", "changed_files": [], "truncated": False, "message": "ok"}
        r_b = {"success": True, "diff": "B", "changed_files": [], "truncated": False, "message": "ok"}
        results = [r_a, r_b]
        with patch.object(
            ToolBackupManager, "_preview_rollback_diff_remote_batch",
            side_effect=lambda *a, **kw: results.pop(0),
        ):
            m1 = _meta(commit="c1"); m1["nest_name"] = "epyc"
            m2 = _meta(commit="c2"); m2["nest_name"] = "epyc"
            assert ToolBackupManager.preview_rollback_diff(m1) is r_a
            assert ToolBackupManager.preview_rollback_diff(m2) is r_b

    def test_cache_capacity_evicts_oldest(self):
        """缓存超32条时淘汰最旧一半"""
        with patch.object(
            ToolBackupManager, "_preview_rollback_diff_remote_batch",
            side_effect=lambda *a, **kw: {"success": True, "diff": "", "changed_files": [], "truncated": False, "message": "ok"},
        ):
            for i in range(33):
                m = _meta(commit=f"c{i:03d}"); m["nest_name"] = "epyc"
                ToolBackupManager.preview_rollback_diff(m)
        assert len(tool_backup._preview_diff_cache) <= 32
