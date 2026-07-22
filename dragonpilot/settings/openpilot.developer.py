"""
Mirror of upstream openpilot's DeveloperLayout settings for the dashy web UI.

See openpilot.toggles.py for the rationale (DASHY-gated, no param_type, etc.).

Notes:
- AlphaLongitudinalEnabled / JoystickDebugMode / LongitudinalManeuverMode are
  hidden on release builds in the device UI (DeveloperLayout._update_toggles).
  Mirror them with `condition: "DASHY and not IS_RELEASE"` so release-branch
  dashy hides them too.
- SSH Keys: rendered as a text_input (GithubUsername) + text_display
  (GithubSshKeys) + clear button. The actual github.com fetch is handled by
  the dashy action endpoint /api/action/ssh_key_set since it has side effects
  (HTTP request, atomic two-param write, error handling) that don't fit a
  declarative "set this param" model.
- "Show Last Errors": text_display of dp_dev_last_log — the device modal is
  also still available via the existing DeveloperLayout button.
"""
from dragonpilot.settings import tr

# Keep the dashy settings schema independent from the native Raylib UI. Importing
# DeveloperLayout just for these strings initializes gui_app at module import time,
# which blocks a headless serverd process on macOS.
_DEV_DESC = {
  "enable_adb": (
    "ADB (Android Debug Bridge) allows connecting to your device over USB or over the network. "
    "See https://docs.comma.ai/how-to/connect-to-comma for more info."
  ),
  "ssh_key": (
    "Warning: This grants SSH access to all public keys in your GitHub settings. Never enter a GitHub username "
    "other than your own. A comma employee will NEVER ask you to add their GitHub username."
  ),
  "alpha_longitudinal": (
    "<b>WARNING: openpilot longitudinal control is in alpha for this car and may disable Automatic Emergency "
    "Braking (AEB).</b><br><br>On this car, openpilot defaults to the car's built-in ACC instead of openpilot's "
    "longitudinal control. Enable this to switch to openpilot longitudinal control. Enabling Experimental mode "
    "is recommended when enabling openpilot longitudinal control alpha. Changing this setting will restart "
    "openpilot if the car is powered on."
  ),
}

_SEC = "Developer"
_DASHY = "DASHY"
_DASHY_ALPHA = "DASHY and not IS_RELEASE"

ITEMS = [
  {
    "section": _SEC, "key": "AdbEnabled", "type": "toggle_item",
    "title": lambda: tr("Enable ADB"),
    "description": lambda: tr(_DEV_DESC["enable_adb"]),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "SshEnabled", "type": "toggle_item",
    "title": lambda: tr("Enable SSH"),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "JoystickDebugMode", "type": "toggle_item",
    "title": lambda: tr("Joystick Debug Mode"),
    "condition": _DASHY_ALPHA,
  },
  {
    "section": _SEC, "key": "LongitudinalManeuverMode", "type": "toggle_item",
    "title": lambda: tr("Longitudinal Maneuver Mode"),
    "condition": _DASHY_ALPHA,
  },
  {
    "section": _SEC, "key": "AlphaLongitudinalEnabled", "type": "toggle_item",
    "title": lambda: tr("openpilot Longitudinal Control (Alpha)"),
    "description": lambda: tr(_DEV_DESC["alpha_longitudinal"]),
    "condition": _DASHY_ALPHA,
  },
  {
    "section": _SEC, "key": "ShowDebugInfo", "type": "toggle_item",
    "title": lambda: tr("UI Debug Mode"),
    "condition": _DASHY,
  },

  # SSH Keys — typed input + display + clear. github.com fetch lives in the
  # dashy ssh_key_set action; see dragonpilot/dashy/serverd.py.
  {
    "section": _SEC, "key": "GithubUsername", "type": "text_input_item",
    "title": lambda: tr("GitHub Username (SSH Keys)"),
    "description": lambda: tr(_DEV_DESC["ssh_key"]),
    "action": "ssh_key_set",
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "GithubSshKeys", "type": "text_display_item",
    "title": lambda: tr("Stored SSH Keys"),
    "condition": _DASHY,
  },
  {
    "section": _SEC, "key": "ssh_key_clear", "type": "action_item",
    "title": lambda: tr("Clear SSH Keys"),
    "action": "ssh_key_clear",
    "condition": _DASHY,
  },

  # Last error log — read-only display of dp_dev_last_log (already declared by
  # core-feat/panel). Device side still has the "Show Last Errors" modal button.
  {
    "section": _SEC, "key": "dp_dev_last_log", "type": "text_display_item",
    "title": lambda: tr("Last Errors"),
    "condition": _DASHY,
  },
]
