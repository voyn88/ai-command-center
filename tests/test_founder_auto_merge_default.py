from command_center.pipeline_settings import PipelineSettings


def test_omitted_auto_merge_key_defaults_on():
    settings = PipelineSettings.from_dict({"enabled": True})
    assert settings.auto_merge_after_checks is True
    assert settings.auto_merge_active is True


def test_explicit_false_still_disables_merge():
    settings = PipelineSettings.from_dict(
        {"enabled": True, "auto_merge_after_checks": False}
    )
    assert settings.auto_merge_after_checks is False
    assert settings.auto_merge_active is False
