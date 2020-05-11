"""Reads vehicle status from BMW connected drive portal."""
import asyncio
import logging

from bimmer_connected.account import ConnectedDriveAccount
from bimmer_connected.country_selector import get_region_from_name
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import track_utc_time_change
import homeassistant.util.dt as dt_util

from .const import CONF_ALLOWED_REGIONS

_LOGGER = logging.getLogger(__name__)

DOMAIN = "bmw_connected_drive"
CONF_REGION = "region"
CONF_READ_ONLY = "read_only"
ATTR_VIN = "vin"

ACCOUNT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_REGION): vol.In(CONF_ALLOWED_REGIONS),
        vol.Optional(CONF_READ_ONLY, default=False): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema({DOMAIN: {cv.string: ACCOUNT_SCHEMA}}, extra=vol.ALLOW_EXTRA)

SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_VIN): cv.string})


BMW_COMPONENTS = ["binary_sensor", "device_tracker", "lock", "notify", "sensor"]
UPDATE_INTERVAL = 5  # in minutes

SERVICE_UPDATE_STATE = "update_state"

_SERVICE_MAP = {
    "light_flash": "trigger_remote_light_flash",
    "sound_horn": "trigger_remote_horn",
    "activate_air_conditioning": "trigger_remote_air_conditioning",
}


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the BMW Connected Drive component from configuration.yaml."""
    if DOMAIN in config:
        for entry_config in list(config[DOMAIN].values()):
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": "import"}, data=entry_config
                )
            )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up BMW Connected Drive from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    try:
        account = await hass.async_add_executor_job(
            setup_account, entry.data, hass, entry.data[CONF_USERNAME]
        )
        await hass.async_add_executor_job(account.update)
    except Exception:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = account

    async def _async_update_all():
        """Update all BMW accounts."""
        await hass.async_add_executor_job(_update_all)

        return True

    def _update_all() -> None:
        """Update all BMW accounts."""
        for cd_account in list(hass.data[DOMAIN].values()):
            cd_account.update()

    # Service to manually trigger updates for all accounts.
    hass.services.async_register(DOMAIN, SERVICE_UPDATE_STATE, _update_all)

    await _async_update_all()

    # for component in BMW_COMPONENTS:
    #     await discovery.async_load_platform(hass, component, DOMAIN, {}, entry.data)

    for platform in BMW_COMPONENTS:
        if platform != "notify":
            hass.async_create_task(
                hass.config_entries.async_forward_entry_setup(entry, platform)
            )

    hass.async_create_task(
        discovery.async_load_platform(hass, "notify", DOMAIN, {}, entry.data)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in BMW_COMPONENTS
            ]
        )
    )

    # Only remove services if it is the last account
    if len(hass.data[DOMAIN]) == 1:
        services = list(_SERVICE_MAP) + [SERVICE_UPDATE_STATE]
        unload_services = all(
            await asyncio.gather(
                *[
                    hass.async_add_executor_job(hass.services.remove, DOMAIN, service)
                    for service in services
                ]
            )
        )
    else:
        unload_services = True

    if all([unload_ok, unload_services]):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def setup_account(account_config: dict, hass, name: str) -> "BMWConnectedDriveAccount":
    """Set up a new BMWConnectedDriveAccount based on the config."""
    username = account_config[CONF_USERNAME]
    password = account_config[CONF_PASSWORD]
    region = account_config[CONF_REGION]
    read_only = account_config[CONF_READ_ONLY]

    _LOGGER.debug("Adding new account %s", name)
    cd_account = BMWConnectedDriveAccount(username, password, region, name, read_only)

    def execute_service(call):
        """Execute a service for a vehicle."""
        vin = call.data[ATTR_VIN]
        vehicle = None
        for account in [
            account for account in hass.data[DOMAIN] if not account.read_only
        ]:
            vehicle = account.get_vehicle(vin)
        if not vehicle:
            _LOGGER.error("Could not find a vehicle for VIN %s", vin)
            return
        function_name = _SERVICE_MAP[call.service]
        function_call = getattr(vehicle.remote_services, function_name)
        function_call()

    if not read_only:
        # register the remote services
        for service in _SERVICE_MAP:
            hass.services.register(
                DOMAIN, service, execute_service, schema=SERVICE_SCHEMA
            )

    # update every UPDATE_INTERVAL minutes, starting now
    # this should even out the load on the servers
    now = dt_util.utcnow()
    track_utc_time_change(
        hass,
        cd_account.update,
        minute=range(now.minute % UPDATE_INTERVAL, 60, UPDATE_INTERVAL),
        second=now.second,
    )

    return cd_account


class BMWConnectedDriveAccount:
    """Representation of a BMW vehicle."""

    def __init__(
        self, username: str, password: str, region_str: str, name: str, read_only
    ) -> None:
        """Initialize account."""
        region = get_region_from_name(region_str)

        self.read_only = read_only
        self.account = ConnectedDriveAccount(username, password, region)
        self.name = name
        self._update_listeners = []

    def update(self, *_):
        """Update the state of all vehicles.

        Notify all listeners about the update.
        """
        _LOGGER.debug(
            "Updating vehicle state for account %s, notifying %d listeners",
            self.name,
            len(self._update_listeners),
        )
        try:
            self.account.update_vehicle_states()
            for listener in self._update_listeners:
                listener()
        except OSError as exception:
            _LOGGER.error(
                "Could not connect to the BMW Connected Drive portal. "
                "The vehicle state could not be updated."
            )
            _LOGGER.exception(exception)

    def add_update_listener(self, listener):
        """Add a listener for update notifications."""
        self._update_listeners.append(listener)
