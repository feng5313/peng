#!/usr/bin/env python3
"""
测试 nest uninstall 后 config/nest.json 的清理逻辑：
1. 卸载指定实例时，如果该实例是默认 nest，应删除 default 字段
2. 卸载所有实例时，应清空 instances 和 default 字段
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from peng.nest.cli import _remove_instance_config, _remove_all_instance_configs


@pytest.fixture
def temp_nest_config(tmp_path):
    """创建临时 nest.json 配置文件并 mock _get_project_root"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "nest.json"

    def _write_config(data: dict):
        config_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _read_config() -> dict:
        if config_file.exists():
            return json.loads(config_file.read_text(encoding="utf-8"))
        return {}

    with patch("peng.nest.cli._get_project_root", return_value=tmp_path):
        yield _write_config, _read_config


class TestRemoveInstanceConfig:
    """测试 _remove_instance_config"""

    def test_remove_instance_clears_default(self, temp_nest_config):
        """卸载的实例是默认 nest 时，应删除 default 字段"""
        write_cfg, read_cfg = temp_nest_config

        write_cfg({
            "instances": {
                "my-nest": {"host": "192.168.1.100", "port": 8899},
                "other-nest": {"host": "10.0.0.1", "port": 8899},
            },
            "default": "my-nest",
        })

        result = _remove_instance_config("my-nest")
        assert result is True

        config = read_cfg()
        assert "my-nest" not in config["instances"]
        assert "other-nest" in config["instances"]
        assert "default" not in config, f"default 字段应被删除，但得到: {config.get('default')}"

    def test_remove_non_default_instance_keeps_default(self, temp_nest_config):
        """卸载的实例不是默认 nest 时，default 字段应保留"""
        write_cfg, read_cfg = temp_nest_config

        write_cfg({
            "instances": {
                "my-nest": {"host": "192.168.1.100", "port": 8899},
                "other-nest": {"host": "10.0.0.1", "port": 8899},
            },
            "default": "my-nest",
        })

        result = _remove_instance_config("other-nest")
        assert result is True

        config = read_cfg()
        assert "other-nest" not in config["instances"]
        assert "my-nest" in config["instances"]
        assert config.get("default") == "my-nest"

    def test_remove_nonexistent_instance(self, temp_nest_config):
        """移除不存在的实例应返回 False"""
        write_cfg, read_cfg = temp_nest_config

        write_cfg({
            "instances": {
                "my-nest": {"host": "192.168.1.100", "port": 8899},
            },
        })

        result = _remove_instance_config("no-such-nest")
        assert result is False

        config = read_cfg()
        assert "my-nest" in config["instances"]

    def test_remove_last_instance_with_default(self, temp_nest_config):
        """卸载唯一的实例（同时也是默认）时，应删除 instance 和 default"""
        write_cfg, read_cfg = temp_nest_config

        write_cfg({
            "instances": {
                "my-nest": {"host": "192.168.1.100", "port": 8899},
            },
            "default": "my-nest",
        })

        result = _remove_instance_config("my-nest")
        assert result is True

        config = read_cfg()
        assert config["instances"] == {}
        assert "default" not in config

    def test_remove_instance_no_default_set(self, temp_nest_config):
        """没有 default 字段时正常移除实例"""
        write_cfg, read_cfg = temp_nest_config

        write_cfg({
            "instances": {
                "my-nest": {"host": "192.168.1.100", "port": 8899},
            },
        })

        result = _remove_instance_config("my-nest")
        assert result is True

        config = read_cfg()
        assert config["instances"] == {}


class TestRemoveAllInstanceConfigs:
    """测试 _remove_all_instance_configs"""

    def test_remove_all_clears_everything(self, temp_nest_config):
        """卸载所有实例时应清空 instances 和 default"""
        write_cfg, read_cfg = temp_nest_config

        write_cfg({
            "instances": {
                "my-nest": {"host": "192.168.1.100", "port": 8899},
                "other-nest": {"host": "10.0.0.1", "port": 8899},
            },
            "default": "my-nest",
        })

        result = _remove_all_instance_configs()
        assert result is True

        config = read_cfg()
        assert config["instances"] == {}
        assert "default" not in config

    def test_remove_all_no_default(self, temp_nest_config):
        """没有 default 字段时也能正常清空"""
        write_cfg, read_cfg = temp_nest_config

        write_cfg({
            "instances": {
                "my-nest": {"host": "192.168.1.100", "port": 8899},
            },
        })

        result = _remove_all_instance_configs()
        assert result is True

        config = read_cfg()
        assert config["instances"] == {}
