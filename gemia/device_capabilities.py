"""Host-owned device capability policy for project design programs.

The model may *request* capabilities, but it never grants them.  This module
keeps that distinction explicit:

* deterministic compute/media acceleration can be enabled inside the normal
  project sandbox;
* privacy-sensitive capture, peripherals and networking remain host-gated;
* macOS TCC consent is always owned by the user and is never reported as
  granted merely because a subprocess was started.

The registry is intentionally independent of any one video, provider or tool
schema so the desktop UI and future device adapters can share the same facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal


AccessMode = Literal["implicit", "sandbox_accelerated", "host_os_managed"]


@dataclass(frozen=True)
class DeviceCapability:
    """One host capability and the authority required to expose it."""

    name: str
    category: str
    description: str
    access_mode: AccessMode


@dataclass(frozen=True)
class DeviceCapabilityPlan:
    """Resolved execution grants for one design-program process."""

    requested: tuple[str, ...]
    active: tuple[str, ...]
    os_managed: tuple[str, ...]
    allow_gpu: bool
    unrestricted_host: bool

    def to_receipt(self) -> dict[str, Any]:
        return {
            "requested": list(self.requested),
            "active": list(self.active),
            "os_managed": list(self.os_managed),
            "sandbox_grants": {
                "gpu_and_media_acceleration": (
                    self.allow_gpu and not self.unrestricted_host
                )
            },
            "authority": (
                "unrestricted_host_os_consent_still_applies"
                if self.unrestricted_host
                else "project_sandbox"
            ),
        }


class DeviceCapabilityPermissionError(PermissionError):
    """A requested capability needs an explicit host/user authority change."""


_CAPABILITIES: tuple[DeviceCapability, ...] = (
    DeviceCapability(
        "project.files",
        "storage",
        "Read and write files inside the current project workspace.",
        "implicit",
    ),
    DeviceCapability(
        "compute.cpu",
        "compute",
        "Use local CPU cores for deterministic design and rendering code.",
        "implicit",
    ),
    DeviceCapability(
        "compute.gpu",
        "compute",
        "Use the local GPU through Metal/IOKit-backed frameworks.",
        "sandbox_accelerated",
    ),
    DeviceCapability(
        "compute.neural",
        "compute",
        "Use local ML acceleration exposed by Core ML and the host hardware.",
        "sandbox_accelerated",
    ),
    DeviceCapability(
        "media.hardware_decode",
        "media",
        "Use platform hardware video decoders when the codec supports it.",
        "sandbox_accelerated",
    ),
    DeviceCapability(
        "media.hardware_encode",
        "media",
        "Use platform hardware video encoders such as VideoToolbox.",
        "sandbox_accelerated",
    ),
    DeviceCapability(
        "graphics.display",
        "graphics",
        "Use local display/graphics frameworks for previews and compositing.",
        "sandbox_accelerated",
    ),
    DeviceCapability(
        "audio.output",
        "audio",
        "Use the host audio output device for monitored playback.",
        "host_os_managed",
    ),
    DeviceCapability(
        "capture.camera",
        "capture",
        "Capture frames from a connected camera.",
        "host_os_managed",
    ),
    DeviceCapability(
        "capture.microphone",
        "capture",
        "Capture audio from a connected microphone.",
        "host_os_managed",
    ),
    DeviceCapability(
        "capture.screen",
        "capture",
        "Capture the screen or application windows.",
        "host_os_managed",
    ),
    DeviceCapability(
        "sensor.location",
        "sensor",
        "Read location data made available by the operating system.",
        "host_os_managed",
    ),
    DeviceCapability(
        "device.bluetooth",
        "peripheral",
        "Communicate with Bluetooth peripherals.",
        "host_os_managed",
    ),
    DeviceCapability(
        "device.usb",
        "peripheral",
        "Communicate with connected USB devices.",
        "host_os_managed",
    ),
    DeviceCapability(
        "device.hid",
        "peripheral",
        "Receive input from HID controllers.",
        "host_os_managed",
    ),
    DeviceCapability(
        "device.midi",
        "peripheral",
        "Use MIDI controllers and endpoints.",
        "host_os_managed",
    ),
    DeviceCapability(
        "network.outbound",
        "network",
        "Open outbound network connections from project code.",
        "host_os_managed",
    ),
    DeviceCapability(
        "network.listen",
        "network",
        "Listen for inbound local/network connections from project code.",
        "host_os_managed",
    ),
)

DEVICE_CAPABILITIES: dict[str, DeviceCapability] = {
    capability.name: capability for capability in _CAPABILITIES
}
DEVICE_CAPABILITY_NAMES: tuple[str, ...] = tuple(DEVICE_CAPABILITIES)
_IMPLICIT = tuple(
    capability.name
    for capability in _CAPABILITIES
    if capability.access_mode == "implicit"
)


def capability_catalog() -> list[dict[str, str]]:
    """Return stable, serializable capability metadata for host/UI adapters."""

    return [
        {
            "name": capability.name,
            "category": capability.category,
            "description": capability.description,
            "access_mode": capability.access_mode,
        }
        for capability in _CAPABILITIES
    ]


def resolve_device_capabilities(
    requested: Iterable[str] | None,
    *,
    unrestricted_host: bool,
) -> DeviceCapabilityPlan:
    """Resolve a model request against host-owned execution authority.

    ``unrestricted_host`` reflects the existing user-controlled sandbox
    setting.  It is deliberately not accepted from tool arguments.  Even in
    that mode, privacy-sensitive entries are labelled ``os_managed`` because
    macOS may still require or deny TCC consent at the point of use.
    """

    normalized: list[str] = []
    for raw in requested or ():
        name = str(raw).strip()
        if not name or name in normalized:
            continue
        normalized.append(name)

    unknown = [name for name in normalized if name not in DEVICE_CAPABILITIES]
    if unknown:
        raise ValueError(
            "unknown device capabilities: "
            + ", ".join(unknown)
            + "; available: "
            + ", ".join(DEVICE_CAPABILITY_NAMES)
        )

    host_managed = tuple(
        name
        for name in normalized
        if DEVICE_CAPABILITIES[name].access_mode == "host_os_managed"
    )
    if host_managed and not unrestricted_host:
        raise DeviceCapabilityPermissionError(
            "device capabilities require explicit host permission: "
            + ", ".join(host_managed)
            + ". Project code cannot grant these permissions; use the existing "
            "host sandbox control, then macOS consent remains authoritative."
        )

    accelerated = tuple(
        name
        for name in normalized
        if DEVICE_CAPABILITIES[name].access_mode == "sandbox_accelerated"
    )
    active = tuple(dict.fromkeys((*_IMPLICIT, *normalized)))
    return DeviceCapabilityPlan(
        requested=tuple(normalized),
        active=active,
        os_managed=host_managed,
        allow_gpu=bool(accelerated),
        unrestricted_host=unrestricted_host,
    )
