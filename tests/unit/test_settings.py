"""Startup refuses configurations that would be dangerous in production."""

from __future__ import annotations

import pytest

from infrastructure.settings import (
    EXAMPLE_SECRET,
    Environment,
    JobExecutionMode,
    Settings,
)

STRONG_SECRET = "a" * 64


class TestRuntimeValidation:
    def test_development_is_permissive(self):
        Settings(env=Environment.DEVELOPMENT).validate_for_runtime()

    def test_production_refuses_the_example_secret(self):
        settings = Settings(env=Environment.PRODUCTION, secret_key=EXAMPLE_SECRET)
        with pytest.raises(RuntimeError, match="QIP_SECRET_KEY"):
            settings.validate_for_runtime()

    def test_production_refuses_a_short_secret(self):
        settings = Settings(env=Environment.PRODUCTION, secret_key="short-secret")
        with pytest.raises(RuntimeError, match="at least 32"):
            settings.validate_for_runtime()

    def test_production_refuses_eager_jobs(self):
        """Eager mode runs long calculations inside the request thread."""
        settings = Settings(
            env=Environment.PRODUCTION,
            secret_key=STRONG_SECRET,
            job_execution_mode=JobExecutionMode.EAGER,
        )
        with pytest.raises(RuntimeError, match="eager"):
            settings.validate_for_runtime()

    def test_a_correct_production_configuration_starts(self):
        Settings(
            env=Environment.PRODUCTION,
            secret_key=STRONG_SECRET,
            job_execution_mode=JobExecutionMode.QUEUE,
        ).validate_for_runtime()

    @pytest.mark.parametrize(
        "env,expected",
        [
            (Environment.DEVELOPMENT, False),
            (Environment.TEST, False),
            (Environment.STAGING, True),
            (Environment.PRODUCTION, True),
        ],
    )
    def test_production_like_classification(self, env, expected):
        assert Settings(env=env).is_production_like is expected


class TestConfigurationSurface:
    def test_log_format_is_constrained(self):
        with pytest.raises(ValueError):
            Settings(log_format="xml")

    def test_every_setting_is_prefixed(self, monkeypatch):
        monkeypatch.setenv("QIP_MAX_UPLOAD_BYTES", "1234")
        assert Settings().max_upload_bytes == 1234
