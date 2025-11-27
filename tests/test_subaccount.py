"""Tests for subaccount validation and enforcement."""

from __future__ import annotations

from unittest.mock import Mock, patch

from hypertrade.routes.hyperliquid_service import HyperliquidService


def test_hyperliquid_service_stores_subaccount_when_provided():
    """Test that HyperliquidService stores subaccount address."""
    subaccount = "0xSUBACCOUNT789"

    with patch("hypertrade.routes.hyperliquid_service.HyperliquidExecutionClient"):
        service = HyperliquidService(
            master_addr="0xMASTER",
            api_wallet_priv="test-key",
            subaccount_addr=subaccount,
        )

        assert service.subaccount_addr == subaccount


def test_hyperliquid_service_stores_none_when_no_subaccount():
    """Test that HyperliquidService stores None when subaccount not provided."""
    with patch("hypertrade.routes.hyperliquid_service.HyperliquidExecutionClient"):
        service = HyperliquidService(
            master_addr="0xMASTER",
            api_wallet_priv="test-key",
            subaccount_addr=None,
        )

        assert service.subaccount_addr is None


def test_hyperliquid_client_initialized_with_subaccount():
    """Test that HyperliquidExecutionClient receives subaccount parameter."""
    subaccount = "0xSUB123"

    with patch(
        "hypertrade.routes.hyperliquid_service.HyperliquidExecutionClient"
    ) as mock_client_class:
        service = HyperliquidService(
            master_addr="0xMASTER",
            api_wallet_priv="test-key",
            subaccount_addr=subaccount,
        )

        # Verify HyperliquidExecutionClient was called with subaccount
        mock_client_class.assert_called_once()
        call_kwargs = mock_client_class.call_args[1]
        assert call_kwargs["vault_address"] == subaccount


def test_hyperliquid_client_initialized_without_subaccount():
    """Test that HyperliquidExecutionClient receives None for vault_address."""
    with patch(
        "hypertrade.routes.hyperliquid_service.HyperliquidExecutionClient"
    ) as mock_client_class:
        service = HyperliquidService(
            master_addr="0xMASTER",
            api_wallet_priv="test-key",
            subaccount_addr=None,
        )

        # Verify HyperliquidExecutionClient was called with None
        mock_client_class.assert_called_once()
        call_kwargs = mock_client_class.call_args[1]
        assert call_kwargs["vault_address"] is None


def test_subaccount_passed_to_execution_client_with_correct_params():
    """Test that all required parameters are passed to HyperliquidExecutionClient."""
    master = "0xMASTER123"
    subaccount = "0xSUB456"
    priv_key = "test-private-key"
    base_url = "https://api.test.hyperliquid.xyz"

    with patch(
        "hypertrade.routes.hyperliquid_service.HyperliquidExecutionClient"
    ) as mock_client_class:
        service = HyperliquidService(
            base_url=base_url,
            master_addr=master,
            api_wallet_priv=priv_key,
            subaccount_addr=subaccount,
        )

        # Verify correct parameters were passed
        mock_client_class.assert_called_once()
        call_kwargs = mock_client_class.call_args[1]
        assert call_kwargs["private_key"] == priv_key
        assert call_kwargs["account_address"] == master
        assert call_kwargs["vault_address"] == subaccount
        assert call_kwargs["base_url"] == base_url
