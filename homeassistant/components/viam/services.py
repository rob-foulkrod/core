"""Services for Viam integration."""

from __future__ import annotations

import base64
from datetime import datetime
from functools import partial

from PIL import Image
from viam.app.app_client import RobotPart
from viam.services.vision import VisionClient
from viam.services.vision.client import RawImage
import voluptuous as vol

from homeassistant.components import camera
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import selector

from .const import DOMAIN
from .manager import ViamManager

ATTR_CONFIG_ENTRY = "config_entry"

DATA_CAPTURE_SERVICE_NAME = "capture_data"
CAPTURE_IMAGE_SERVICE_NAME = "capture_image"
CLASSIFICATION_SERVICE_NAME = "get_classifications"
DETECTIONS_SERVICE_NAME = "get_detections"

SERVICE_VALUES = "values"
SERVICE_COMPONENT_NAME = "component_name"
SERVICE_COMPONENT_TYPE = "component_type"
SERVICE_FILEPATH = "filepath"
SERVICE_CAMERA = "camera"
SERVICE_CONFIDENCE = "confidence_threshold"
SERVICE_ROBOT_ADDRESS = "robot_address"
SERVICE_ROBOT_SECRET = "robot_secret"
SERVICE_FILE_NAME = "file_name"
SERVICE_CLASSIFIER_NAME = "classifier_name"
SERVICE_COUNT = "count"
SERVICE_DETECTOR_NAME = "detector_name"

ENTRY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY): selector.ConfigEntrySelector(
            {
                "integration": DOMAIN,
            }
        ),
    }
)
DATA_CAPTURE_SERVICE_SCHEMA = ENTRY_SERVICE_SCHEMA.extend(
    {
        vol.Required(SERVICE_VALUES): vol.All(dict),
        vol.Required(SERVICE_COMPONENT_NAME): vol.All(str),
        vol.Required(SERVICE_COMPONENT_TYPE, default="sensor"): vol.All(str),
    }
)

IMAGE_SERVICE_FIELDS = ENTRY_SERVICE_SCHEMA.extend(
    {
        vol.Optional(SERVICE_FILEPATH): vol.All(str, vol.IsFile),
        vol.Optional(SERVICE_CAMERA): vol.All(str),
    }
)
VISION_SERVICE_FIELDS = IMAGE_SERVICE_FIELDS.extend(
    {
        vol.Optional(SERVICE_CONFIDENCE, default="0.6"): vol.All(
            str, vol.Coerce(float), vol.Range(min=0, max=1)
        ),
        vol.Optional(SERVICE_ROBOT_ADDRESS): vol.All(str),
        vol.Optional(SERVICE_ROBOT_SECRET): vol.All(str),
    }
)

CAPTURE_IMAGE_SERVICE_SCHEMA = IMAGE_SERVICE_FIELDS.extend(
    {
        vol.Optional(SERVICE_FILE_NAME, default="camera"): vol.All(str),
        vol.Optional(SERVICE_COMPONENT_NAME): vol.All(str),
    }
)

CLASSIFICATION_SERVICE_SCHEMA = VISION_SERVICE_FIELDS.extend(
    {
        vol.Required(SERVICE_CLASSIFIER_NAME): vol.All(str),
        vol.Optional(SERVICE_COUNT, default="2"): vol.All(str, vol.Coerce(int)),
    }
)

DETECTIONS_SERVICE_SCHEMA = VISION_SERVICE_FIELDS.extend(
    {
        vol.Required(SERVICE_DETECTOR_NAME): vol.All(str),
    }
)


def __fetch_image(filepath: str | None) -> Image.Image | None:
    if filepath is None:
        return None
    return Image.open(filepath)


def __encode_image(image: Image.Image | RawImage) -> str:
    """Create base64-encoded Image string."""
    if isinstance(image, Image.Image):
        image_bytes = image.tobytes()
    else:  # RawImage
        image_bytes = image.data

    image_string = base64.b64encode(image_bytes).decode()
    return f"data:image/jpeg;base64,{image_string}"


async def __get_image(
    hass: HomeAssistant, filepath: str | None, camera_entity: str | None
) -> RawImage | Image.Image | None:
    """Retrieve image type from camera entity or file system."""
    if filepath is not None:
        return await hass.async_add_executor_job(__fetch_image, filepath)
    if camera_entity is not None:
        image = await camera.async_get_image(hass, camera_entity)
        return RawImage(image.content, image.content_type)

    return None


def __get_manager(hass: HomeAssistant, call: ServiceCall) -> ViamManager:
    entry_id: str = call.data[ATTR_CONFIG_ENTRY]
    entry: ConfigEntry | None = hass.config_entries.async_get_entry(entry_id)

    if not entry:
        raise ServiceValidationError(
            f"Invalid config entry: {entry_id}",
            translation_domain=DOMAIN,
            translation_key="invalid_config_entry",
            translation_placeholders={
                "config_entry": entry_id,
            },
        )
    if entry.state != ConfigEntryState.LOADED:
        raise ServiceValidationError(
            f"{entry.title} is not loaded",
            translation_domain=DOMAIN,
            translation_key="unloaded_config_entry",
            translation_placeholders={
                "config_entry": entry.title,
            },
        )

    manager: ViamManager = hass.data[DOMAIN][entry_id]
    return manager


async def __capture_data(call: ServiceCall, *, hass: HomeAssistant) -> None:
    """Accept input from service call to send to Viam."""
    manager: ViamManager = __get_manager(hass, call)
    parts: list[RobotPart] = await manager.get_robot_parts()
    values = [call.data.get(SERVICE_VALUES, {})]
    component_type = call.data.get(SERVICE_COMPONENT_TYPE, "sensor")
    component_name = call.data.get(SERVICE_COMPONENT_NAME, "")

    await manager.viam.data_client.tabular_data_capture_upload(
        tabular_data=values,
        part_id=parts.pop().id,
        component_type=component_type,
        component_name=component_name,
        method_name="capture_data",
        data_request_times=[(datetime.now(), datetime.now())],
    )


async def __capture_image(call: ServiceCall, *, hass: HomeAssistant) -> None:
    """Accept input from service call to send to Viam."""
    manager: ViamManager = __get_manager(hass, call)
    parts: list[RobotPart] = await manager.get_robot_parts()
    filepath = call.data.get(SERVICE_FILEPATH)
    camera_entity = call.data.get(SERVICE_CAMERA)
    component_name = call.data.get(SERVICE_COMPONENT_NAME)
    file_name = call.data.get(SERVICE_FILE_NAME, "camera")

    if filepath is not None:
        await manager.viam.data_client.file_upload_from_path(
            filepath=filepath,
            part_id=parts.pop().id,
            component_name=component_name,
        )
    if camera_entity is not None:
        image = await camera.async_get_image(hass, camera_entity)
        await manager.viam.data_client.file_upload(
            part_id=parts.pop().id,
            component_name=component_name,
            file_name=file_name,
            file_extension=".jpeg",
            data=image.content,
        )


async def __get_service_values(
    hass: HomeAssistant, call: ServiceCall, service_config_name: str
):
    """Create common values for vision services."""
    manager: ViamManager = __get_manager(hass, call)
    filepath = call.data.get(SERVICE_FILEPATH)
    camera_entity = call.data.get(SERVICE_CAMERA)
    service_name = call.data.get(service_config_name, "")
    count = int(call.data.get(SERVICE_COUNT, 2))
    confidence_threshold = float(call.data.get(SERVICE_CONFIDENCE, 0.6))

    async with await manager.get_robot_client(
        call.data.get(SERVICE_ROBOT_SECRET), call.data.get(SERVICE_ROBOT_ADDRESS)
    ) as robot:
        service: VisionClient = VisionClient.from_robot(robot, service_name)
        image = await __get_image(hass, filepath, camera_entity)

    return manager, service, image, filepath, confidence_threshold, count


async def __get_classifications(
    call: ServiceCall, *, hass: HomeAssistant
) -> ServiceResponse:
    """Accept input configuration to request classifications."""
    (
        manager,
        classifier,
        image,
        filepath,
        confidence_threshold,
        count,
    ) = await __get_service_values(hass, call, SERVICE_CLASSIFIER_NAME)

    if image is None:
        return {
            "classifications": [],
            "img_src": filepath or None,
        }

    img_src = filepath or __encode_image(image)
    classifications = await classifier.get_classifications(image, count)

    return {
        "classifications": [
            {"name": c.class_name, "confidence": c.confidence}
            for c in classifications
            if c.confidence >= confidence_threshold
        ],
        "img_src": img_src,
    }


async def __get_detections(
    call: ServiceCall, *, hass: HomeAssistant
) -> ServiceResponse:
    """Accept input configuration to request detections."""
    (
        manager,
        detector,
        image,
        filepath,
        confidence_threshold,
        _count,
    ) = await __get_service_values(hass, call, SERVICE_DETECTOR_NAME)

    if image is None:
        return {
            "detections": [],
            "img_src": filepath or None,
        }

    img_src = filepath or __encode_image(image)
    detections = await detector.get_detections(image)

    return {
        "detections": [
            {
                "name": c.class_name,
                "confidence": c.confidence,
                "x_min": c.x_min,
                "y_min": c.y_min,
                "x_max": c.x_max,
                "y_max": c.y_max,
            }
            for c in detections
            if c.confidence >= confidence_threshold
        ],
        "img_src": img_src,
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Viam integration."""

    hass.services.async_register(
        DOMAIN,
        DATA_CAPTURE_SERVICE_NAME,
        partial(__capture_data, hass=hass),
        DATA_CAPTURE_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        CAPTURE_IMAGE_SERVICE_NAME,
        partial(__capture_image, hass=hass),
        CAPTURE_IMAGE_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        CLASSIFICATION_SERVICE_NAME,
        partial(__get_classifications, hass=hass),
        CLASSIFICATION_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        DETECTIONS_SERVICE_NAME,
        partial(__get_detections, hass=hass),
        DETECTIONS_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
