"""fix(20): 0.7.0 -> 0.8.0 按角色推断 model_type。"""

from neobot_app.config.migrations import migrate_v7_to_v8


def _models(registry, assignments):
    return {"registry": registry, "assignments": assignments}


def test_infers_vision_type():
    old = {
        "version": "0.7.0",
        "models": _models(
            [
                {"model_ref": "vl-1", "model_name": "vl"},
                {"model_ref": "chat-1", "model_name": "chat", "model_type": "chat"},
            ],
            {"vision_model": "vl-1", "primary_chat_model": "chat-1"},
        ),
    }
    new = migrate_v7_to_v8(old)
    types = {e["model_ref"]: e.get("model_type") for e in new["models"]["registry"]}
    assert types["vl-1"] == "vision"
    assert types["chat-1"] == "chat"
    assert new["version"] == "0.8.0"


def test_mixed_or_explicit_or_unreferenced_unchanged():
    old = {
        "version": "0.7.0",
        "models": _models(
            [
                {"model_ref": "mixed", "model_type": "chat"},
                {"model_ref": "explicit", "model_type": "tts"},
                {"model_ref": "unused"},
            ],
            {
                "vision_model": "mixed",
                "tts_model": "mixed",
                "creator_image_models": "explicit",
            },
        ),
    }
    new = migrate_v7_to_v8(old)
    types = {e["model_ref"]: e.get("model_type") for e in new["models"]["registry"]}
    assert types["mixed"] == "chat"
    assert types["explicit"] == "tts"
    assert types["unused"] is None


def test_without_registry_keeps_models():
    old = {"version": "0.7.0", "models": {"assignments": {"vision_model": "x"}}}
    new = migrate_v7_to_v8(old)
    assert new["models"] == old["models"]
