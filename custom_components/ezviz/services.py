"""Services for the EZVIZ integration."""

import probatio

from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv, service

from .const import ATTR_EVENT_TIME, DOMAIN, SERVICE_GET_CLIP_URL, SERVICE_WAKE_DEVICE


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the services for the EZVIZ integration."""

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_WAKE_DEVICE,
        entity_domain=CAMERA_DOMAIN,
        schema=None,
        func="perform_wake_device",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_GET_CLIP_URL,
        entity_domain=CAMERA_DOMAIN,
        schema={probatio.Optional(ATTR_EVENT_TIME): cv.datetime},
        func="async_get_clip_url",
        supports_response=SupportsResponse.ONLY,
    )
