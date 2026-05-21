"""get_video_call_media_level() 单元测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config


@pytest.fixture(autouse=True)
def _reset_settings():
    """每个测试前后还原 SETTINGS 状态"""
    orig_override = config.SETTINGS.get("video_call_media_override")
    orig_model = config.SETTINGS.get("default_model")
    yield
    if orig_override is None:
        config.SETTINGS.pop("video_call_media_override", None)
    else:
        config.SETTINGS["video_call_media_override"] = orig_override
    if orig_model is None:
        config.SETTINGS.pop("default_model", None)
    else:
        config.SETTINGS["default_model"] = orig_model


class TestGetVideoCallMediaLevel:

    def test_override_video(self):
        config.SETTINGS["video_call_media_override"] = "video"
        assert config.get_video_call_media_level() == "video"

    def test_override_image(self):
        config.SETTINGS["video_call_media_override"] = "image"
        assert config.get_video_call_media_level() == "image"

    def test_override_text(self):
        config.SETTINGS["video_call_media_override"] = "text"
        assert config.get_video_call_media_level() == "text"

    def test_override_invalid_falls_through(self):
        config.SETTINGS["video_call_media_override"] = "invalid"
        result = config.get_video_call_media_level()
        assert result in ("video", "image", "text")

    def test_override_none_falls_through(self):
        config.SETTINGS["video_call_media_override"] = None
        result = config.get_video_call_media_level()
        assert result in ("video", "image", "text")

    def test_model_with_media_field(self):
        config.SETTINGS.pop("video_call_media_override", None)
        for name, cfg in config.MODELS.items():
            if "media" in cfg:
                config.SETTINGS["default_model"] = name
                assert config.get_video_call_media_level() == cfg["media"]
                return
        pytest.skip("no model with media field")

    def test_model_without_media_defaults_text(self):
        config.SETTINGS.pop("video_call_media_override", None)
        config.MODELS["_test_no_media"] = {"provider": "test", "model": "test"}
        try:
            config.SETTINGS["default_model"] = "_test_no_media"
            assert config.get_video_call_media_level() == "text"
        finally:
            del config.MODELS["_test_no_media"]

    def test_unknown_model_defaults_text(self):
        config.SETTINGS.pop("video_call_media_override", None)
        config.SETTINGS["default_model"] = "nonexistent-model-xyz"
        assert config.get_video_call_media_level() == "text"

    def test_override_takes_priority_over_model(self):
        config.SETTINGS["video_call_media_override"] = "text"
        for name, cfg in config.MODELS.items():
            if cfg.get("media") == "video":
                config.SETTINGS["default_model"] = name
                assert config.get_video_call_media_level() == "text"
                return
        pytest.skip("no video-capable model")

    def test_all_models_have_media_field(self):
        """确认所有 MODELS 都已标注 media 字段"""
        missing = [name for name, cfg in config.MODELS.items() if "media" not in cfg]
        assert missing == [], f"以下模型缺少 media 字段: {missing}"
