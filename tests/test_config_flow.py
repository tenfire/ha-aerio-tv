"""Tests for the AerioTV config flow."""

from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from voluptuous_serialize import convert

from custom_components.aeriotv import config_flow
from custom_components.aeriotv.const import CONF_DEVICE_ID, CONF_PORT, CONF_TOKEN, DOMAIN


def discovery(device_id: str, host: str, port: int, name: str) -> ZeroconfServiceInfo:
    """Build real Home Assistant zeroconf discovery data."""
    address = ip_address(host)
    return ZeroconfServiceInfo(
        ip_address=address,
        ip_addresses=[address],
        port=port,
        hostname=f"{name.lower()}.local.",
        type="_aeriotv._tcp.local.",
        name=f"{name}._aeriotv._tcp.local.",
        properties={"id": device_id, "v": "1"},
    )


async def test_zeroconf_uses_stable_id_and_pairs(hass: HomeAssistant, enable_custom_integrations) -> None:
    """Discovery creates an entry identified by the stable TV ID."""
    with (
        patch.object(
            config_flow.AerioTVClient,
            "begin_pairing",
            new=AsyncMock(),
        ),
        patch.object(
            config_flow.AerioTVClient,
            "submit_code",
            new=AsyncMock(return_value="secret-token"),
        ),
        patch.object(
            config_flow.AerioTVClient,
            "disconnect",
            new=AsyncMock(),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery("tv-stable-id", "192.0.2.10", 43123, "Living Room"),
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "confirm"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "pair"
        convert(result["data_schema"])

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room"
    assert result["result"].unique_id == "tv-stable-id"
    assert result["data"] == {
        CONF_HOST: "192.0.2.10",
        CONF_PORT: 43123,
        CONF_DEVICE_ID: "tv-stable-id",
        CONF_TOKEN: "secret-token",
    }


async def test_user_setup_requires_discovery(hass: HomeAssistant, enable_custom_integrations) -> None:
    """User-initiated setup does not expose an ephemeral manual endpoint form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "discovery_required"


async def test_pair_form_rejects_non_digit_code_locally(hass: HomeAssistant, enable_custom_integrations) -> None:
    """A malformed six-character code remains on the form without a socket send."""
    submit_code = AsyncMock()
    with (
        patch.object(config_flow.AerioTVClient, "begin_pairing", new=AsyncMock()),
        patch.object(config_flow.AerioTVClient, "submit_code", new=submit_code),
        patch.object(config_flow.AerioTVClient, "disconnect", new=AsyncMock()),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery("tv-stable-id", "192.0.2.10", 43123, "Living Room"),
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "12AB56"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert result["errors"] == {"base": "invalid_auth"}
    submit_code.assert_not_awaited()


async def test_rediscovery_updates_ephemeral_endpoint(hass: HomeAssistant, enable_custom_integrations) -> None:
    """Rediscovery updates host and port without creating a duplicate entry."""
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Living Room",
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 40000,
            CONF_DEVICE_ID: "tv-stable-id",
            CONF_TOKEN: "secret-token",
        },
        source=config_entries.SOURCE_ZEROCONF,
        unique_id="tv-stable-id",
        discovery_keys={},
        options={},
        subentries_data=[],
    )
    await hass.config_entries.async_add(entry)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery("tv-stable-id", "192.0.2.20", 49999, "Living Room"),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.0.2.20"
    assert entry.data[CONF_PORT] == 49999


async def test_reauth_replaces_token_on_existing_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    """A revoked token can be replaced without replacing the config entry."""
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Living Room",
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 43123,
            CONF_DEVICE_ID: "tv-stable-id",
            CONF_TOKEN: "revoked-token",
        },
        source=config_entries.SOURCE_ZEROCONF,
        unique_id="tv-stable-id",
        discovery_keys={},
        options={},
        subentries_data=[],
    )
    await hass.config_entries.async_add(entry)

    disconnect = AsyncMock()
    with (
        patch.object(
            config_flow.AerioTVClient,
            "begin_pairing",
            new=AsyncMock(),
        ),
        patch.object(
            config_flow.AerioTVClient,
            "submit_code",
            new=AsyncMock(return_value="fresh-token"),
        ),
        patch.object(
            config_flow.AerioTVClient,
            "disconnect",
            new=disconnect,
        ),
        patch.object(
            hass.config_entries,
            "async_reload",
            new=AsyncMock(return_value=True),
        ) as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=dict(entry.data),
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "pair"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "fresh-token"
    assert entry.entry_id in {configured.entry_id for configured in hass.config_entries.async_entries(DOMAIN)}
    disconnect.assert_awaited()
    reload_entry.assert_awaited_once_with(entry.entry_id)
