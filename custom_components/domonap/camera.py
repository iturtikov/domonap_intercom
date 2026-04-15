import logging
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    StreamType,
)
from homeassistant.core import callback

try:
    from homeassistant.components.camera import (
        WebRTCAnswer,
        WebRTCClientConfiguration,
        WebRTCError,
        WebRTCSendMessage,
    )
except ImportError:
    WebRTCAnswer = None
    WebRTCClientConfiguration = None
    WebRTCError = None
    WebRTCSendMessage = None

from .const import DOMAIN, API

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WHEPMedia:
    media: str
    mid: str


@dataclass(frozen=True)
class WHEPOfferData:
    ice_ufrag: str
    ice_pwd: str
    medias: list[WHEPMedia]


@dataclass(frozen=True)
class WHEPSession:
    session_url: str
    offer_data: WHEPOfferData


async def async_setup_entry(hass, config_entry, async_add_entities):
    entities = []
    api = hass.data[DOMAIN][config_entry.entry_id][API]
    response = await api.get_paged_keys()
    keys = response.get("results", [])
    for key in keys:
        key_id = key["id"]
        if (
            key.get("httpVideoUrl") is not None
            or key.get("webrtcVideoUrl") is not None
        ):
            camera_class = (
                IntercomWebRTCCamera if key.get("webrtcVideoUrl") else IntercomCamera
            )
            entities.append(
                camera_class(
                    api,
                    key_id,
                    key["name"],
                    key.get("httpVideoUrl"),
                    key.get("videoPreview"),
                    key,
                )
            )

    async_add_entities(entities, True)


class IntercomCamera(Camera):
    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_frontend_stream_type = StreamType.HLS
    _attr_motion_detection_enabled = False
    _attr_translation_key = "camera"

    def __init__(
        self,
        api,
        key_id: str,
        name: str,
        stream_url: str | None,
        snapshot_url: str | None,
        key_data: dict,
    ):
        super().__init__()
        self._api = api
        self._key_id = key_id
        self._name = name
        self._stream_url = stream_url
        self._snapshot_url = snapshot_url
        self._key_data = key_data

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._key_data

    @property
    def unique_id(self):
        return self._key_id

    async def async_camera_image(self, width=None, height=None):
        if self._snapshot_url is None:
            return None

        response = await self._api.fetch_external_bytes(self._snapshot_url)
        if response["ok"]:
            _LOGGER.debug(f"Successfully fetched snapshot for {self._name}")
            return response["body"]

        _LOGGER.error(
            "Failed to fetch snapshot from %s: %s",
            self._snapshot_url,
            response.get("error"),
        )
        return None

    async def stream_source(self):
        return self._stream_url

    @property
    def supported_features(self):
        return self._attr_supported_features

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._key_id)},
            "name": self._name,
            "manufacturer": "Domonap",
            "model": "Intercom Device",
            "via_device": (DOMAIN, self._key_id),
        }

    async def async_update(self):
        _LOGGER.debug(f"Updating camera: {self._name}")


class IntercomWebRTCCamera(IntercomCamera):
    """Domonap camera with native WebRTC/WHEP playback support."""

    _attr_frontend_stream_type = getattr(StreamType, "WEB_RTC", StreamType.HLS)

    def __init__(
        self,
        api,
        key_id: str,
        name: str,
        stream_url: str | None,
        snapshot_url: str | None,
        key_data: dict,
    ):
        super().__init__(api, key_id, name, stream_url, snapshot_url, key_data)
        self._webrtc_url = key_data["webrtcVideoUrl"]
        self._whep_url = _whep_url_from_webrtc_url(self._webrtc_url)
        self._webrtc_sessions: dict[str, WHEPSession] = {}
        self._pending_candidates = defaultdict(list)

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle a Home Assistant WebRTC offer through Domonap's WHEP endpoint."""
        if WebRTCAnswer is None or WebRTCError is None:
            _LOGGER.error("Home Assistant WebRTC API is not available")
            return

        offer_data = _parse_offer_sdp(offer_sdp)
        response = await self._api.create_whep_session(self._whep_url, offer_sdp)
        if not response["ok"]:
            send_message(
                WebRTCError(
                    "domonap_webrtc_offer_failed",
                    f"Domonap WHEP offer failed: {response.get('error')}",
                )
            )
            return

        self._webrtc_sessions[session_id] = WHEPSession(
            session_url=urljoin(self._whep_url, response["location"]),
            offer_data=offer_data,
        )
        send_message(WebRTCAnswer(response["answer_sdp"]))

        pending_candidates = self._pending_candidates.pop(session_id, [])
        if pending_candidates:
            await self._async_send_webrtc_candidates(session_id, pending_candidates)

    @callback
    def _async_get_webrtc_client_configuration(self):
        """Return client-side WebRTC options expected by MediaMTX."""
        if WebRTCClientConfiguration is None:
            return super()._async_get_webrtc_client_configuration()

        return WebRTCClientConfiguration(data_channel="domonap")

    async def async_on_webrtc_candidate(self, session_id: str, candidate) -> None:
        """Forward a WebRTC ICE candidate to Domonap's WHEP session."""
        if not getattr(candidate, "candidate", None):
            return

        if session_id not in self._webrtc_sessions:
            self._pending_candidates[session_id].append(candidate)
            return

        await self._async_send_webrtc_candidates(session_id, [candidate])

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a Domonap WHEP session."""
        self._pending_candidates.pop(session_id, None)
        whep_session = self._webrtc_sessions.pop(session_id, None)
        if whep_session is None:
            return

        self.hass.async_create_task(
            self._async_close_whep_session(whep_session.session_url)
        )

    async def _async_send_webrtc_candidates(self, session_id: str, candidates) -> None:
        """Send local ICE candidates to the active WHEP session."""
        whep_session = self._webrtc_sessions.get(session_id)
        if whep_session is None:
            return

        sdp_fragment = _generate_sdp_fragment(whep_session.offer_data, candidates)
        if not sdp_fragment:
            return

        response = await self._api.send_whep_candidates(
            whep_session.session_url,
            sdp_fragment,
        )
        if not response["ok"]:
            _LOGGER.warning(
                "Domonap WHEP candidate failed for %s: %s %s",
                self._name,
                response.get("error"),
                response.get("body", "")[:200],
            )

    async def _async_close_whep_session(self, session_url: str) -> None:
        """Close the WHEP session on the Domonap side."""
        response = await self._api.close_whep_session(session_url)
        if not response["ok"]:
            _LOGGER.debug(
                "Domonap WHEP session close failed for %s: %s",
                self._name,
                response.get("error"),
            )


def _parse_offer_sdp(offer_sdp: str) -> WHEPOfferData:
    """Extract ICE data and media sections needed for WHEP trickle ICE."""
    ice_ufrag = ""
    ice_pwd = ""
    medias: list[WHEPMedia] = []

    for line in offer_sdp.split("\r\n"):
        if line.startswith("m="):
            medias.append(WHEPMedia(media=line[2:], mid=str(len(medias))))
        elif line.startswith("a=mid:") and medias:
            medias[-1] = WHEPMedia(media=medias[-1].media, mid=line[6:])
        elif not ice_ufrag and line.startswith("a=ice-ufrag:"):
            ice_ufrag = line[len("a=ice-ufrag:"):]
        elif not ice_pwd and line.startswith("a=ice-pwd:"):
            ice_pwd = line[len("a=ice-pwd:"):]

    return WHEPOfferData(ice_ufrag=ice_ufrag, ice_pwd=ice_pwd, medias=medias)


def _whep_url_from_webrtc_url(webrtc_url: str) -> str:
    """Return the MediaMTX WHEP endpoint URL for a Domonap WebRTC page URL."""
    parsed = urlsplit(webrtc_url)
    path = parsed.path.rstrip("/") + "/whep"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _generate_sdp_fragment(offer_data: WHEPOfferData, candidates) -> str | None:
    """Build the SDP fragment expected by MediaMTX/WHEP for ICE candidates."""
    candidates_by_media = defaultdict(list)

    for candidate in candidates:
        candidate_value = getattr(candidate, "candidate", None)
        if not candidate_value:
            continue

        media_index = _candidate_media_index(offer_data, candidate)
        if media_index is None:
            continue
        if media_index < 0 or media_index >= len(offer_data.medias):
            continue

        candidates_by_media[media_index].append(candidate_value)

    if not candidates_by_media:
        return None

    fragment = f"a=ice-ufrag:{offer_data.ice_ufrag}\r\n"
    if offer_data.ice_pwd:
        fragment += f"a=ice-pwd:{offer_data.ice_pwd}\r\n"

    for media_index, media in enumerate(offer_data.medias):
        if media_index not in candidates_by_media:
            continue

        fragment += f"m={media.media}\r\n"
        fragment += f"a=mid:{media.mid}\r\n"
        for candidate_value in candidates_by_media[media_index]:
            fragment += f"a={candidate_value}\r\n"

    return fragment


def _candidate_media_index(offer_data: WHEPOfferData, candidate) -> int | None:
    """Return the media index for a WebRTC candidate."""
    media_index = getattr(candidate, "sdp_m_line_index", None)
    if media_index is not None:
        return media_index

    candidate_mid = getattr(candidate, "sdp_mid", None)
    if candidate_mid is None:
        return 0 if offer_data.medias else None

    for index, media in enumerate(offer_data.medias):
        if media.mid == candidate_mid:
            return index

    if str(candidate_mid).isdigit():
        return int(candidate_mid)

    return None
