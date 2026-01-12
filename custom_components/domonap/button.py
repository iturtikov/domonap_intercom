import logging
from homeassistant.components.button import ButtonEntity
from .const import DOMAIN, API

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    entities = []
    api = hass.data[DOMAIN][config_entry.entry_id][API]
    response = await api.get_paged_keys()
    keys = response.get("results", [])
    for key in keys:
        key_id = key["id"]
        door_id = key["doorId"]
        door_name = key["name"]
        entities.append(IntercomDoor(api, key_id, door_id, door_name, key))

    async_add_entities(entities, True)


class IntercomDoor(ButtonEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:lock"
    _attr_translation_key = "open_door"

    def __init__(self, api, key_id, door_id: str, name: str, key_data: dict):
        self._api = api
        self._key_id = key_id
        self._door_id = door_id
        self._name = name
        self._key_data = key_data

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._key_data

    @property
    def unique_id(self):
        return self._door_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._key_id)},
            "name": self._name,
            "manufacturer": "Domonap",
            "model": "Intercom Device",
            "via_device": (DOMAIN, self._key_id),
        }

    async def async_press(self):
        try:
            response = await self._api.open_relay_by_key_id(self._key_id)
            if response.get('ok') is not True:
                _LOGGER.error(f"Failed to open the door {self._name}. Response: {response}")
        except Exception as e:
            _LOGGER.error(f"Error opening the door {self._name}: {e}")