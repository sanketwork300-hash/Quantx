"""What the feed worker refuses to do.

The worker's most important behaviour is not what it streams; it is what it
declines to start. A market-data process that quietly serves generated prices,
or that holds a socket open decoding nothing, is worse than one that exits with
a reason — because both of those look like a working system.
"""

from __future__ import annotations

import pytest

from apps.stream.main import StreamNotRunnable, build_transport, run
from domains.market_data.streaming.feed import PollingFeedTransport
from infrastructure.settings import Settings


class _Provider:
    async def raw_quotes(self, keys):  # pragma: no cover - never called here
        return {}


async def _token() -> str:  # pragma: no cover - never called here
    return "unused"


def _settings(**overrides) -> Settings:
    base = {
        "market_data_provider": "upstox",
        "market_stream_transport": "polling",
        "market_data_account_email": "desk@example.com",
    }
    return Settings(**{**base, **overrides})


class TestChoosingATransport:
    def test_polling_is_the_transport_that_needs_nothing_extra(self):
        transport = build_transport(_settings(), _Provider(), _token)
        assert isinstance(transport, PollingFeedTransport)
        assert transport.delivers_every_update is False

    def test_the_poll_interval_comes_from_configuration(self):
        transport = build_transport(
            _settings(market_stream_poll_interval_seconds=2.5), _Provider(), _token
        )
        assert transport.interval_seconds == 2.5

    def test_a_websocket_without_a_frame_decoder_refuses_to_start(self):
        """A connection that stays up and delivers zero quotes is the failure
        this refusal exists to prevent — it looks exactly like a quiet market."""
        with pytest.raises(StreamNotRunnable) as exc:
            build_transport(_settings(market_stream_transport="websocket"), _Provider(), _token)
        assert "QIP_UPSTOX_FEED_PROTO_MODULE" in str(exc.value)
        assert "polling" in str(exc.value)

    def test_a_websocket_whose_decoder_module_is_absent_says_how_to_make_one(self):
        with pytest.raises(StreamNotRunnable, match="protoc|protobuf"):
            build_transport(
                _settings(
                    market_stream_transport="websocket",
                    upstox_feed_proto_module="no.such.generated.module",
                ),
                _Provider(),
                _token,
            )

    def test_an_unrecognised_transport_name_is_refused(self):
        with pytest.raises(StreamNotRunnable, match="polling"):
            build_transport(
                _settings(market_stream_transport="carrier-pigeon"), _Provider(), _token
            )


class TestWhatTheWorkerWillNotDo:
    async def test_it_refuses_to_run_against_the_synthetic_market(self):
        """Build spec 48.4. A feed worker publishing a generated market into the
        live store would put invented prices behind every live endpoint."""
        with pytest.raises(StreamNotRunnable, match="upstox"):
            await run(_settings(market_data_provider="synthetic"))

    async def test_it_refuses_to_pick_a_market_data_account_for_you(self, app_environment):
        """One entitlement serves the deployment, so which account it belongs to
        is a decision with licensing consequences, not a default."""
        with pytest.raises(StreamNotRunnable, match="QIP_MARKET_DATA_ACCOUNT_EMAIL"):
            await run(_settings(market_data_account_email=None))

    async def test_an_account_that_does_not_exist_is_named(self, app_environment):
        with pytest.raises(StreamNotRunnable, match="nobody@example.com"):
            await run(_settings(market_data_account_email="nobody@example.com"))


class TestChoosingAProvider:
    """Where the synthetic market is allowed and where it is not.

    The refusal is at construction rather than at startup on purpose. A
    deployment that only analyses uploaded chains never builds a provider, and
    refusing to start it would be refusing something legitimate. What must never
    happen is the synthetic market being *served* as real, and that happens
    here.
    """

    def _synthetic_config(self):
        from datetime import UTC, datetime

        from domains.market_data.providers.synthetic import SyntheticMarketConfig

        return SyntheticMarketConfig(as_of=datetime(2026, 9, 9, tzinfo=UTC))

    def test_the_synthetic_market_is_available_in_development(self):
        from domains.market_data.providers.factory import ProviderKind, build_provider

        provider = build_provider(ProviderKind.SYNTHETIC, synthetic_config=self._synthetic_config())
        assert provider.name == "synthetic"

    def test_the_synthetic_market_is_refused_in_a_production_like_environment(self):
        from domains.market_data.providers.factory import (
            ProviderKind,
            ProviderNotAvailable,
            build_provider,
        )

        with pytest.raises(ProviderNotAvailable, match="describe nothing real"):
            build_provider(
                ProviderKind.SYNTHETIC,
                synthetic_config=self._synthetic_config(),
                production_like=True,
            )

    def test_a_live_provider_that_cannot_be_built_fails_rather_than_falling_back(self):
        """Build spec 48.4 and 48.5. A silent fallback to generated prices is
        the worst outcome available to this code."""
        from domains.market_data.providers.factory import (
            ProviderKind,
            ProviderNotAvailable,
            build_provider,
        )

        with pytest.raises(ProviderNotAvailable, match="credential"):
            build_provider(ProviderKind.UPSTOX)

    def test_a_file_only_production_deployment_still_starts(self):
        """It never builds a provider, so the synthetic default is irrelevant to
        it and must not block startup."""
        from infrastructure.settings import Environment, JobExecutionMode

        Settings(
            env=Environment.PRODUCTION,
            secret_key="x" * 40,
            job_execution_mode=JobExecutionMode.QUEUE,
        ).validate_for_runtime()
