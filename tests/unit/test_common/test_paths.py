"""
测试 paths.py 模块
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peng.common.paths import (
    get_working_directory,
    is_in_working_directory,
    is_project_file,
    is_temporary_directory,
    is_protected_project_path,
)


class TestGetWorkingDirectory:
    """测试get_working_directory函数"""

    @patch("peng.common.paths.load_config")
    def test_get_working_directory_from_config(self, mock_load_config):
        """测试从配置获取工作目录"""
        with tempfile.TemporaryDirectory() as temp_dir:
            workdir = Path(temp_dir) / "workspace"
            workdir.mkdir()

            mock_config = MagicMock()
            mock_config.webui.workdir = str(workdir)
            mock_load_config.return_value = mock_config

            result = get_working_directory()
            assert result == workdir

    @patch("peng.common.paths.load_config")
    def test_get_working_directory_fallback_to_cwd(self, mock_load_config):
        """测试配置加载失败时回退到当前目录"""
        mock_load_config.side_effect = Exception("Config error")

        result = get_working_directory()
        assert result == Path.cwd()

    @patch("peng.common.paths.load_config")
    def test_get_working_directory_invalid_dir(self, mock_load_config):
        """测试配置目录不存在时回退到当前目录"""
        mock_config = MagicMock()
        mock_config.webui.workdir = "/nonexistent/path"
        mock_load_config.return_value = mock_config

        result = get_working_directory()
        assert result == Path.cwd()


class TestIsInWorkingDirectory:
    """测试is_in_working_directory函数"""

    def test_file_in_working_directory(self):
        """测试文件在工作目录内"""
        workdir = Path("/tmp/test_work")
        file_path = Path("/tmp/test_work/subdir/file.txt")

        with patch("peng.common.paths.get_working_directory", return_value=workdir):
            assert is_in_working_directory(file_path) is True

    def test_file_outside_working_directory(self):
        """测试文件在工作目录外"""
        workdir = Path("/tmp/test_work")
        file_path = Path("/other/path/file.txt")

        with patch("peng.common.paths.get_working_directory", return_value=workdir):
            assert is_in_working_directory(file_path) is False

    def test_temp_path_exception(self):
        """测试临时路径的例外情况"""
        workdir = Path("/tmp/test_work")
        temp_path = Path("/tmp/tmpXXXXXX/file.txt")

        with patch("peng.common.paths.get_working_directory", return_value=workdir):
            with patch("peng.common.paths.is_temporary_directory", return_value=True):
                assert is_in_working_directory(temp_path) is False


class TestIsProjectFile:
    """测试is_project_file函数"""

    @patch("peng.common.paths.load_config")
    def test_is_project_file_true(self, mock_load_config):
        """测试项目文件"""
        project_root = Path("/tmp/project")
        file_path = Path("/tmp/project/src/main.py")

        mock_config = MagicMock()
        mock_config.paths.project_root_path = project_root
        mock_load_config.return_value = mock_config

        assert is_project_file(file_path) is True

    @patch("peng.common.paths.load_config")
    def test_is_project_file_outside(self, mock_load_config):
        """测试非项目文件"""
        project_root = Path("/tmp/project")
        file_path = Path("/other/path/file.txt")

        mock_config = MagicMock()
        mock_config.paths.project_root_path = project_root
        mock_load_config.return_value = mock_config

        assert is_project_file(file_path) is False


class TestIsTemporaryDirectory:
    """测试is_temporary_directory函数"""

    def test_is_temporary_directory_tmp(self):
        """测试/tmp目录"""
        assert is_temporary_directory(Path("/tmp")) is True

    def test_is_temporary_directory_false(self):
        """测试非临时目录"""
        assert is_temporary_directory(Path("/home/user/project")) is False


class TestIsProtectedProjectPath:
    """测试is_protected_project_path函数"""

    @patch("peng.common.paths.load_config")
    def test_is_protected_project_path_true(self, mock_load_config):
        """测试受保护路径"""
        project_root = Path("/project")
        file_path = Path("/project/src/main.py")

        mock_config = MagicMock()
        mock_config.paths.project_root_path = project_root
        mock_config.paths.allowed_tool_call_dirs_paths = []
        mock_load_config.return_value = mock_config

        assert is_protected_project_path(file_path) is True

    @patch("peng.common.paths.load_config")
    def test_is_protected_project_path_allowed_dir(self, mock_load_config):
        """测试允许的目录"""
        project_root = Path("/project")
        allowed_dir = Path("/project/allowed")
        file_path = Path("/project/allowed/file.txt")

        mock_config = MagicMock()
        mock_config.paths.project_root_path = project_root
        mock_config.paths.allowed_tool_call_dirs_paths = [allowed_dir]
        mock_load_config.return_value = mock_config

        assert is_protected_project_path(file_path) is False

    @patch("peng.common.paths.load_config")
    def test_is_protected_project_path_outside(self, mock_load_config):
        """测试项目外路径"""
        project_root = Path("/project")
        file_path = Path("/other/path/file.txt")

        mock_config = MagicMock()
        mock_config.paths.project_root_path = project_root
        mock_config.paths.allowed_tool_call_dirs_paths = []
        mock_load_config.return_value = mock_config

        assert is_protected_project_path(file_path) is False
