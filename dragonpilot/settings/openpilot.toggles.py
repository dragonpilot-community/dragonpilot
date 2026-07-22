"""
Mirror of upstream openpilot's TogglesLayout settings for the dashy web UI.

The device's own Toggles panel remains the source of truth for the device touchscreen.
These entries surface the same params in dashy so a user on a small-screen device
(comma4 / mici) can flip them from their phone instead.

Items are gated by `condition: "DASHY"` so they only render in the web context —
dashy's settings-context dict sets DASHY=True; the device dp panel leaves it unset
(falsy), hiding these to avoid duplicating the existing Toggles tab.

No flags/param_type/default on items: the params are owned by upstream openpilot
and already declared in common/params_keys.h. generate_settings.py won't emit
duplicates because these have no flags+param_type pair.

Drift detection: see system/tests/test_openpilot_mirror.py.
"""
from dragonpilot.settings import tr

# Keep this web schema importable without initializing the native Raylib UI.
_TOGGLES_DESC = {
  "OpenpilotEnabledToggle": "Use the openpilot system for adaptive cruise control and lane keep driver assistance. Your attention is required at all times to use this feature.",
  "DisengageOnAccelerator": "When enabled, pressing the accelerator pedal will disengage openpilot.",
  "LongitudinalPersonality": "Standard is recommended. In aggressive mode, openpilot will follow lead cars closer and be more aggressive with the gas and brake. In relaxed mode openpilot will stay further away from lead cars. On supported cars, you can cycle through these personalities with your steering wheel distance button.",
  "IsLdwEnabled": "Receive alerts to steer back into the lane when your vehicle drifts over a detected lane line without a turn signal activated while driving over 31 mph (50 km/h).",
  "AlwaysOnDM": "Enable driver monitoring even when openpilot is not engaged.",
  "RecordFront": "Upload data from the driver facing camera and help improve the driver monitoring algorithm.",
  "IsMetric": "Display speed in km/h instead of mph.",
  "RecordAudio": "Record and store microphone audio while driving. The audio will be included in the dashcam video in comma connect.",
  "DisableLogging": "Disable logging service",
  "DisableUpdates": "Disable update service",
}

_SEC = "Openpilot"
_DASHY = "DASHY"

ITEMS = [
  {
    "section": _SEC, "key": "OpenpilotEnabledToggle", "type": "toggle_item",
    "title": lambda: tr("Enable openpilot"),
    "description": lambda: tr(_TOGGLES_DESC["OpenpilotEnabledToggle"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "ExperimentalMode", "type": "toggle_item",
    "title": lambda: tr("Experimental Mode"),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "DisengageOnAccelerator", "type": "toggle_item",
    "title": lambda: tr("Disengage on Accelerator Pedal"),
    "description": lambda: tr(_TOGGLES_DESC["DisengageOnAccelerator"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "IsLdwEnabled", "type": "toggle_item",
    "title": lambda: tr("Enable Lane Departure Warnings"),
    "description": lambda: tr(_TOGGLES_DESC["IsLdwEnabled"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "AlwaysOnDM", "type": "toggle_item",
    "title": lambda: tr("Always-On Driver Monitoring"),
    "description": lambda: tr(_TOGGLES_DESC["AlwaysOnDM"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "RecordFront", "type": "toggle_item",
    "title": lambda: tr("Record and Upload Driver Camera"),
    "description": lambda: tr(_TOGGLES_DESC["RecordFront"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "RecordAudio", "type": "toggle_item",
    "title": lambda: tr("Record and Upload Microphone Audio"),
    "description": lambda: tr(_TOGGLES_DESC["RecordAudio"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "IsMetric", "type": "toggle_item",
    "title": lambda: tr("Use Metric System"),
    "description": lambda: tr(_TOGGLES_DESC["IsMetric"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "DisableLogging", "type": "toggle_item",
    "title": lambda: tr("Disable Logging"),
    "description": lambda: tr(_TOGGLES_DESC["DisableLogging"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "DisableUpdates", "type": "toggle_item",
    "title": lambda: tr("Disable Updates"),
    "description": lambda: tr(_TOGGLES_DESC["DisableUpdates"]),
    "condition": _DASHY,
  },
  # Longitudinal driving personality — text_spin selector mirroring upstream's multi-button widget.
  {
    "section": _SEC, "key": "LongitudinalPersonality", "type": "text_spin_button_item",
    "title": lambda: tr("Driving Personality"),
    "description": lambda: tr(_TOGGLES_DESC["LongitudinalPersonality"]),
    "options": [lambda: tr("Aggressive"), lambda: tr("Standard"), lambda: tr("Relaxed")],
    "default": "1",  # Standard
    "condition": _DASHY,
  },
]
