#!/usr/bin/env python3

"""
Copyright (c) 2026, Rick Lan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, and/or sublicense,
for non-commercial purposes only, subject to the following conditions:

- The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.
- Commercial use (e.g. use in a product, service, or activity intended to
  generate revenue) is prohibited without explicit written permission from
  the copyright holder.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

Dashy HTTP Server

Provides REST API and static file serving for the dashy web UI.
- Settings management (read/write params)
- File browser for drive logs
- Static file serving for web UI
"""

import argparse
import ast
import asyncio
import json
import operator
import os
import logging
import time
from datetime import datetime
from functools import wraps
from urllib.parse import quote

from aiohttp import web

from cereal import messaging

from openpilot.common.params import Params
from openpilot.system.hardware import PC, HARDWARE
from openpilot.system.ui.lib.multilang import multilang as base_multilang
from dragonpilot.settings import SETTINGS

try:
    from openpilot.system.version import get_build_metadata as _get_build_metadata
except Exception:
    _get_build_metadata = None

# --- Configuration ---
DEFAULT_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), '..') if PC else '/data/media/0/realdata')
WEB_DIST_PATH = os.path.join(os.path.dirname(__file__), "web", "dist")
CAR_PARAMS_CACHE_TTL = 30  # seconds

logger = logging.getLogger("dashy")


class MockParams:
    """In-memory params mock for dev mode."""
    _store = {}
    def get(self, key, default=None): return self._store.get(key, default)
    def get_bool(self, key, default=False): return bool(self._store.get(key)) if key in self._store else default
    def put(self, key, value): self._store[key] = value
    def put_bool(self, key, value): self._store[key] = value
    def remove(self, key): self._store.pop(key, None)
    def check_key(self, key): return True


# --- Caching Layer ---
class AppCache:
    """Centralized cache for expensive operations."""

    def __init__(self, car_brand=None, openpilot_longitudinal_control=False):
        self._params = None
        self._car_brand_override = car_brand
        self._openpilot_longitudinal_control_override = openpilot_longitudinal_control
        self._car_params = None
        self._car_params_time = 0
        self._context = None
        self._context_time = 0
        self._settings_cache = None
        self._settings_cache_time = 0

    @property
    def params(self):
        """Get shared Params instance (or mock if unavailable)."""
        if self._params is None:
            try:
                self._params = Params()
            except Exception as e:
                logger.warning(f"Params unavailable, using mock: {e}")
                self._params = MockParams()
        return self._params

    def get_car_params(self):
        """Get cached CarParams data (brand, longitudinal control)."""
        now = time.time()
        if self._car_params is None or (now - self._car_params_time) > CAR_PARAMS_CACHE_TTL:
            self._car_params = self._parse_car_params()
            self._car_params_time = now
        return self._car_params

    def _parse_car_params(self):
        """Parse CarParams from Params store."""
        if self._car_brand_override:
            return {
                'brand': self._car_brand_override,
                'openpilot_longitudinal_control': self._openpilot_longitudinal_control_override,
            }

        result = {'brand': '', 'openpilot_longitudinal_control': False}
        try:
            # CarParams is cleared offroad/at boot; CarParamsPersistent keeps the last car's
            # params so brand/longitudinal-gated settings still show when configuring parked.
            car_params_bytes = self.params.get("CarParamsPersistent") or self.params.get("CarParams")
            if car_params_bytes:
                from cereal import car
                with car.CarParams.from_bytes(car_params_bytes) as cp:
                    result['brand'] = cp.brand
                    result['openpilot_longitudinal_control'] = cp.openpilotLongitudinalControl
        except Exception as e:
            logger.debug(f"Could not parse CarParams: {e}")
        return result

    def get_settings_context(self):
        """Get context dict for settings condition evaluation."""
        now = time.time()
        if self._context is None or (now - self._context_time) > CAR_PARAMS_CACHE_TTL:
            car_params = self.get_car_params()
            self._context = {
                'brand': car_params['brand'],
                'openpilotLongitudinalControl': car_params['openpilot_longitudinal_control'],
                'LITE': os.getenv("LITE") is not None,
                'MICI': self._check_mici(),
                # Upstream-mirror items gate on these.
                'DASHY': True,
                'IS_RELEASE': self._is_release_channel(),
            }
            self._context_time = now
        return self._context

    def _check_mici(self):
        """Check if device is MICI type."""
        try:
            return HARDWARE.get_device_type() == "mici"
        except Exception:
            return False

    def _is_release_channel(self):
        if _get_build_metadata is None:
            return False
        try:
            return bool(_get_build_metadata().release_channel)
        except Exception:
            return False

    def get_bool_safe(self, key, default=False):
        """Safely get a boolean param with default."""
        try:
            return self.params.get_bool(key)
        except Exception:
            return default

    def invalidate(self):
        """Invalidate all caches."""
        self._car_params = None
        self._context = None
        self._settings_cache = None


# --- Helper Functions ---
def api_handler(func):
    """Decorator for API handlers with consistent error handling."""
    @wraps(func)
    async def wrapper(request):
        try:
            return await func(request)
        except web.HTTPException:
            raise
        except Exception as e:
            logger.error(f"{func.__name__} error: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)
    return wrapper


def get_safe_path(requested_path):
    """Ensures the requested path is within DEFAULT_DIR."""
    combined_path = os.path.join(DEFAULT_DIR, requested_path.lstrip('/'))
    safe_path = os.path.realpath(combined_path)
    if os.path.commonpath((safe_path, DEFAULT_DIR)) == DEFAULT_DIR:
        return safe_path
    return None


_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _eval_node(node, context):
    """Evaluate a tightly restricted AST node against a context dict.

    Only the operators that SETTINGS conditions actually use are supported:
    Name lookup, literal Constants, and / or / not, and the six numeric
    comparisons. No function calls, attribute access, subscripts, or
    arithmetic — those would re-open the eval-sandbox escape paths.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return context.get(node.id, False)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, context)
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, context) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        op_type = type(node.ops[0])
        if op_type in _CMP_OPS:
            left = _eval_node(node.left, context)
            right = _eval_node(node.comparators[0], context)
            return _CMP_OPS[op_type](left, right)
    raise ValueError(f"Unsupported node: {type(node).__name__}")


def eval_condition(condition, context):
    """Evaluate a SETTINGS condition expression in a sandboxed AST walker."""
    if not condition:
        return True
    try:
        tree = ast.parse(condition, mode='eval')
        return bool(_eval_node(tree, context))
    except Exception as e:
        logger.debug(f"Condition evaluation failed: {condition}, error: {e}")
        return False


def resolve_value(value):
    """Resolve callable values (lambdas) for JSON serialization."""
    return value() if callable(value) else value


# Map of settings-declared param keys to their setting dict.
# Used as an allowlist for /api/settings/params/{name} read/write so
# LAN clients can only touch keys that the UI knowingly exposes.
def _build_param_setting_map():
    out = {}
    for section in SETTINGS:
        for setting in section.get('settings', []):
            key = setting.get('key')
            if not key:
                continue
            # action_item entries use `key` as the action name, not a real
            # param — skip so they don't leak into the param read/write
            # allowlist.
            if setting.get('type') == 'action_item':
                continue
            out[key] = setting
    return out


_PARAM_SETTINGS = _build_param_setting_map()

# Control-tab / one-off params the UI legitimately reads or writes that
# are not part of the SETTINGS schema. Kept as an explicit allowlist so
# the broader 'unknown param' guard still blocks arbitrary writes.
_CONTROL_PARAMS = {
    'dp_dev_go_off_road',   # Controls tab: force-offroad toggle
    'DoReboot',             # Controls tab: reboot button
    'ExperimentalMode',     # Tesla HUD: tap set-speed circle to toggle
}


def _param_allowed(key):
    return key in _PARAM_SETTINGS or key in _CONTROL_PARAMS


# --- API Endpoints ---
@api_handler
async def init_api(request):
    """Provide initial data to the client."""
    cache: AppCache = request.app['cache']
    return web.json_response({
        'dp_dev_dashy': cache.get_bool_safe("dp_dev_dashy", True),
        'isOffroad': cache.get_bool_safe("IsOffroad", False),
    })


@api_handler
async def list_files_api(request):
    """List files and folders."""
    path_param = request.query.get('path', '/')
    safe_path = get_safe_path(path_param)

    if not safe_path or not os.path.isdir(safe_path):
        return web.json_response({'error': 'Invalid or Not Found Path'}, status=404)

    items = []
    for entry in os.listdir(safe_path):
        full_path = os.path.join(safe_path, entry)
        # Skip entries whose real target escapes DEFAULT_DIR (e.g., symlinks).
        # get_safe_path only validates the requested directory itself; each
        # child has to be re-checked to prevent listing files outside the tree.
        real_full = os.path.realpath(full_path)
        if os.path.commonpath((real_full, DEFAULT_DIR)) != DEFAULT_DIR:
            continue
        try:
            stat = os.stat(full_path)
            is_dir = os.path.isdir(full_path)
            items.append({
                'name': entry,
                'is_dir': is_dir,
                'mtime': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                'size': stat.st_size if not is_dir else 0
            })
        except FileNotFoundError:
            continue

    # Sort: directories first (by mtime desc), then files (by mtime desc)
    dirs = sorted([i for i in items if i['is_dir']], key=lambda x: x['mtime'], reverse=True)
    files = sorted([i for i in items if not i['is_dir']], key=lambda x: x['mtime'], reverse=True)

    relative_path = os.path.relpath(safe_path, DEFAULT_DIR)
    return web.json_response({
        'path': '' if relative_path == '.' else relative_path,
        'files': dirs + files
    })


@api_handler
async def serve_player_api(request):
    """Serve the HLS player page."""
    file_path = request.query.get('file')
    if not file_path:
        return web.Response(text="File parameter is required.", status=400)
    if get_safe_path(file_path) is None:
        return web.Response(text="Invalid file path.", status=400)

    player_html_path = os.path.join(WEB_DIST_PATH, 'pages', 'player.html')
    try:
        with open(player_html_path, 'r') as f:
            html_template = f.read()
    except FileNotFoundError:
        return web.Response(text="Player HTML not found.", status=500)

    html = html_template.replace('{{FILE_PATH}}', quote(file_path, safe=''))
    return web.Response(text=html, content_type='text/html')


@api_handler
async def serve_manifest_api(request):
    """Dynamically generate m3u8 playlist."""
    file_path = request.query.get('file', '').lstrip('/')
    if not file_path:
        return web.Response(text="File parameter is required.", status=400)
    if get_safe_path(file_path) is None:
        return web.Response(text="Invalid file path.", status=400)

    encoded_path = quote(file_path)
    manifest = f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:60\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:60.0,\n/media/{encoded_path}\n#EXT-X-ENDLIST\n"
    return web.Response(text=manifest, content_type='application/vnd.apple.mpegurl')


@api_handler
async def get_settings_config_api(request):
    """Get the settings configuration from settings.py."""
    cache: AppCache = request.app['cache']

    # Return cached settings if fresh (2 second TTL)
    now = time.time()
    if cache._settings_cache is not None and (now - cache._settings_cache_time) < 2:
        return web.json_response(cache._settings_cache)

    params = cache.params

    # Update language if changed
    current_lang = params.get("LanguageSetting")
    if current_lang:
        lang_str = current_lang.decode() if isinstance(current_lang, bytes) else str(current_lang)
        lang_str = lang_str.removeprefix("main_")
        if lang_str != base_multilang.language and lang_str in base_multilang.languages.values():
            base_multilang._language = lang_str
            base_multilang.setup()

    context = cache.get_settings_context()
    settings_with_values = []

    for section in SETTINGS:
        if not eval_condition(section.get('condition'), context):
            continue

        section_copy = section.copy()
        settings_list = []

        for setting in section.get('settings', []):
            if not eval_condition(setting.get('condition'), context):
                continue

            setting_copy = setting.copy()
            key = setting['key']

            # Resolve callable values
            for field in ['title', 'description', 'suffix', 'special_value_text']:
                if field in setting_copy:
                    setting_copy[field] = resolve_value(setting_copy[field])
            if 'options' in setting_copy:
                setting_copy['options'] = [resolve_value(opt) for opt in setting_copy['options']]

            # Get current value based on type
            setting_copy['current_value'] = _get_setting_value(params, setting)
            settings_list.append(setting_copy)

        if settings_list:
            section_copy['settings'] = settings_list
            settings_with_values.append(section_copy)

    response_data = {'settings': settings_with_values}
    cache._settings_cache = response_data
    cache._settings_cache_time = now
    return web.json_response(response_data)


def _get_setting_value(params, setting):
    """Get current value for a setting from Params."""
    key = setting['key']
    setting_type = setting['type']
    default = setting.get('default', 0)

    try:
        if setting_type == 'toggle_item':
            return params.get_bool(key)
        elif setting_type == 'double_spin_button_item':
            value = params.get(key)
            return float(value) if value is not None else float(default)
        elif setting_type in ('text_input_item', 'text_display_item'):
            value = params.get(key)
            if value is None:
                return ''
            return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value)
        elif setting_type == 'action_item':
            # Pure action buttons have no stored value; return None so the
            # UI treats it as display-only.
            return None
        else:  # spin_button_item, text_spin_button_item
            value = params.get(key)
            return int(value) if value is not None else int(default)
    except Exception as e:
        logger.warning(f"Error getting value for {key}: {e}")
        if setting_type == 'toggle_item':
            return False
        elif setting_type == 'double_spin_button_item':
            return float(default)
        elif setting_type in ('text_input_item', 'text_display_item'):
            return ''
        elif setting_type == 'action_item':
            return None
        return int(default)


@api_handler
async def save_param_api(request):
    """Save a single param value.

    Usage: POST /api/settings/params/{name}
    Body: { "value": <value> }
    """
    param_name = request.match_info.get('param_name')
    if not param_name:
        return web.json_response({'error': 'param_name is required'}, status=400)
    if not _param_allowed(param_name):
        return web.json_response({'error': 'Unknown param'}, status=403)

    setting = _PARAM_SETTINGS.get(param_name)
    if setting is not None and setting.get('type') == 'text_display_item':
        return web.json_response({'error': 'Read-only param'}, status=403)

    cache: AppCache = request.app['cache']
    params = cache.params
    data = await request.json()

    if 'value' not in data:
        return web.json_response({'error': 'value is required in body'}, status=400)

    _save_param(params, param_name, data['value'])
    cache.invalidate()
    logger.info(f"Param saved: {param_name}={data['value']}")

    return web.json_response({'status': 'success', 'key': param_name, 'value': data['value']})


def _save_param(params, key, value):
    """Save a single param value with proper type handling."""
    try:
        param_type = params.get_type(key)

        if param_type == 1:  # BOOL
            params.put_bool(key, bool(value))
        elif param_type == 2:  # INT
            params.put(key, int(value))
        elif param_type == 3:  # FLOAT
            params.put(key, float(value))
        elif isinstance(value, bool):
            params.put_bool(key, value)
        else:
            params.put(key, str(value) if not isinstance(value, str) else value)

        logger.debug(f"Saved {key}={value} (type={param_type})")
    except Exception as e:
        logger.error(f"Error saving param {key}={value}: {e}")
        raise


def _get_param_value(params, key):
    """Get a single param value via its declared setting type, or as a
    bool for control-only params that have no SETTINGS entry."""
    setting = _PARAM_SETTINGS.get(key)
    if setting is not None:
        return _get_setting_value(params, setting)
    if key in _CONTROL_PARAMS:
        try:
            return params.get_bool(key)
        except Exception:
            return False
    return None


@api_handler
async def get_param_api(request):
    """Get a single param value."""
    param_name = request.match_info.get('param_name')
    if not param_name:
        return web.json_response({'error': 'param_name is required'}, status=400)
    if not _param_allowed(param_name):
        return web.json_response({'error': 'Unknown param'}, status=403)

    cache: AppCache = request.app['cache']
    try:
        value = _get_param_value(cache.params, param_name)
    except Exception:
        value = None

    return web.json_response({'key': param_name, 'value': value})


# --- Action endpoints ---
# Named side-effectful operations declared by settings items via the
# `action` field (text_input_item / action_item). Each handler receives
# the parsed JSON body and the AppCache; it returns a dict that is
# serialized as the JSON response. Errors should be raised — the wrapper
# converts them to 502/500 responses.
SSH_KEY_FETCH_TIMEOUT_S = 10
SSH_KEY_MAX_BYTES = 16 * 1024  # plenty for any realistic ~/.ssh/authorized_keys
GITHUB_USERNAME_MAX_LEN = 39   # github's own limit


def _validate_github_username(username):
    """GitHub username: 1-39 chars, alnum or single hyphen, no leading/trailing hyphen."""
    if not username or len(username) > GITHUB_USERNAME_MAX_LEN:
        return False
    if username.startswith('-') or username.endswith('-'):
        return False
    if '--' in username:
        return False
    return all(c.isalnum() or c == '-' for c in username)


async def _fetch_github_ssh_keys(username):
    """Fetch https://github.com/{username}.keys. Returns the body text on
    HTTP 200; raises web.HTTPException with an upstream-derived status on
    failure so the action endpoint surfaces the real reason."""
    import aiohttp
    url = f"https://github.com/{quote(username, safe='')}.keys"
    timeout = aiohttp.ClientTimeout(total=SSH_KEY_FETCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status == 404:
                raise web.HTTPNotFound(reason=f"GitHub user '{username}' not found")
            if resp.status != 200:
                raise web.HTTPBadGateway(reason=f"github.com returned HTTP {resp.status}")
            body = await resp.content.read(SSH_KEY_MAX_BYTES + 1)
            if len(body) > SSH_KEY_MAX_BYTES:
                raise web.HTTPBadGateway(reason="SSH key response too large")
            return body.decode('utf-8', errors='replace')


async def _action_ssh_key_set(request, payload, cache):
    """Fetch the user's GitHub SSH keys and write both params atomically.
    Body: { "value": "<github-username>" }. On success the device's
    sshd_config drop-in is updated by openpilot's own SSH manager."""
    username = (payload.get('value') or '').strip()
    if not _validate_github_username(username):
        raise web.HTTPBadRequest(reason="Invalid GitHub username")

    keys_body = await _fetch_github_ssh_keys(username)
    if not keys_body.strip():
        raise web.HTTPBadRequest(reason=f"GitHub user '{username}' has no public SSH keys")

    params = cache.params
    # Write keys first; only commit the username if keys were stored
    # successfully — keeps the two params consistent.
    params.put('GithubSshKeys', keys_body)
    params.put('GithubUsername', username)
    cache.invalidate()
    logger.info(f"SSH keys set from github.com/{username} ({len(keys_body)} bytes)")
    return {'status': 'ok', 'username': username, 'key_bytes': len(keys_body)}


async def _action_ssh_key_clear(request, payload, cache):
    params = cache.params
    params.put('GithubSshKeys', '')
    params.put('GithubUsername', '')
    cache.invalidate()
    logger.info("SSH keys cleared")
    return {'status': 'ok'}


_ACTION_HANDLERS = {
    'ssh_key_set': _action_ssh_key_set,
    'ssh_key_clear': _action_ssh_key_clear,
}


@api_handler
async def run_action_api(request):
    """Dispatch /api/action/{name} → registered handler."""
    name = request.match_info.get('name', '')
    handler = _ACTION_HANDLERS.get(name)
    if handler is None:
        return web.json_response({'error': f'Unknown action: {name}'}, status=404)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    cache: AppCache = request.app['cache']
    result = await handler(request, payload, cache)
    return web.json_response(result)


@api_handler
async def get_model_list_api(request):
    """Get the model list and current selection."""
    cache: AppCache = request.app['cache']
    params = cache.params

    # Get model list. JSON-typed params come back already-parsed in
    # newer dragonpilot; older builds returned bytes/str — handle both.
    model_list = {}
    try:
        raw = params.get("dp_dev_model_list")
        if raw:
            if isinstance(raw, (bytes, str)):
                model_list = json.loads(raw)
            elif isinstance(raw, dict):
                model_list = raw
    except Exception as e:
        logger.debug(f"Could not parse dp_dev_model_list: {e}")

    # Get current selection
    selected_model = ""
    try:
        selected_raw = params.get("dp_dev_model_selected")
        if selected_raw:
            selected_model = selected_raw.decode('utf-8') if isinstance(selected_raw, bytes) else str(selected_raw)
    except Exception as e:
        logger.debug(f"Could not get dp_dev_model_selected: {e}")

    return web.json_response({
        'model_list': model_list,
        'selected_model': selected_model
    })


@api_handler
async def save_model_selection_api(request):
    """Save the selected model."""
    cache: AppCache = request.app['cache']
    params = cache.params
    data = await request.json()

    selected_model = data.get('selected_model', '')

    if not selected_model or selected_model == "[AUTO]":
        params.put("dp_dev_model_selected", "")
        logger.info("Model selection cleared (AUTO mode)")
    else:
        params.put("dp_dev_model_selected", selected_model)
        logger.info(f"Model selection saved: {selected_model}")

    return web.json_response({'status': 'success'})


# --- WebSocket endpoint for data streaming ---
# One shared publisher task polls the dashyState SubMaster and fans out
# to every connected client. The previous per-connection design ran
# blocking ZMQ I/O on the event loop, which starved every other request
# under multi-client load.
async def _publisher_loop(app):
    # IMPORTANT: ZMQ sockets are thread-affined. Construct the SubMaster on
    # the asyncio main thread and call update() on the same thread — using
    # asyncio.to_thread bounces between worker threads and silently breaks
    # the receive. The 0-timeout update is cheap enough on the event loop;
    # the per-client send is what we actually need to be async for.
    try:
        sm = messaging.SubMaster(['dashyState'])
    except Exception as e:
        logger.warning(f"Publisher disabled (SubMaster init failed): {e}")
        return

    logger.info("dashyState publisher loop started")

    while True:
        try:
            sm.update(0)
            if sm.updated['dashyState']:
                json_data = sm['dashyState'].json
                if isinstance(json_data, bytes):
                    json_data = json_data.decode('utf-8')

                clients = list(app['ws_clients'])
                for ws in clients:
                    if ws.closed:
                        app['ws_clients'].discard(ws)
                        continue
                    try:
                        await ws.send_str(json_data)
                    except Exception as e:
                        logger.debug(f"WebSocket send failed, dropping client: {e}")
                        app['ws_clients'].discard(ws)

            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Don't let a transient error tear down the loop silently.
            logger.exception(f"Publisher loop error: {e}")
            await asyncio.sleep(0.1)


async def websocket_handler(request):
    """WebSocket endpoint for data-only connections - streams dashyState directly."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    logger.info("WebSocket client connected")
    request.app['ws_clients'].add(ws)

    try:
        # Wait until the client disconnects; no inbound traffic expected.
        async for _ in ws:
            pass
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
    finally:
        request.app['ws_clients'].discard(ws)
        logger.info("WebSocket client disconnected")

    return ws


# --- No-cache middleware for web assets ---
# Dashy is a same-origin LAN app; no CORS headers are emitted so that
# browsers will block cross-origin JS from mutating settings via the
# JSON endpoints (the preflight will fail for non-simple requests).
@web.middleware
async def no_cache_middleware(request, handler):
    response = await handler(request)

    path = request.path.lower()
    if path.endswith(('.html', '.js', '.css')) or path == '/':
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'

    return response


# --- Application Setup ---
async def on_startup(app):
    """Initialize app-level resources."""
    app['cache'] = AppCache(
        car_brand=app['car_brand'],
        openpilot_longitudinal_control=app['openpilot_longitudinal_control'],
    )
    app['ws_clients'] = set()
    app['publisher_task'] = asyncio.create_task(_publisher_loop(app))
    logger.info("Dashy server started")


async def on_cleanup(app):
    """Cleanup app-level resources."""
    task = app.get('publisher_task')
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    logger.info("Dashy server stopped")


def setup_aiohttp_app(host: str, port: int, debug: bool, car_brand=None,
                      openpilot_longitudinal_control=False):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    app = web.Application(middlewares=[no_cache_middleware])
    app['car_brand'] = car_brand
    app['openpilot_longitudinal_control'] = openpilot_longitudinal_control

    # API routes
    app.router.add_get("/api/init", init_api)
    app.router.add_get("/api/files", list_files_api)
    app.router.add_get("/api/play", serve_player_api)
    app.router.add_get("/api/manifest.m3u8", serve_manifest_api)
    app.router.add_get("/api/settings", get_settings_config_api)
    app.router.add_get("/api/settings/params/{param_name}", get_param_api)
    app.router.add_post("/api/settings/params/{param_name}", save_param_api)
    app.router.add_get("/api/models", get_model_list_api)
    app.router.add_post("/api/models/select", save_model_selection_api)
    app.router.add_post("/api/action/{name}", run_action_api)
    app.router.add_get("/api/ws", websocket_handler)  # WebSocket for data streaming

    # Static files
    app.router.add_static('/media', path=DEFAULT_DIR, name='media', show_index=False, follow_symlinks=False)
    app.router.add_static('/download', path=DEFAULT_DIR, name='download', show_index=False, follow_symlinks=False)
    app.router.add_get("/", lambda r: web.FileResponse(os.path.join(WEB_DIST_PATH, "index.html")))
    app.router.add_static("/", path=WEB_DIST_PATH)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


def main():
    parser = argparse.ArgumentParser(description="Dashy Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to listen on")
    parser.add_argument("--port", type=int, default=5088, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("--car-brand", type=str, help="Override the car brand for desktop UI testing")
    parser.add_argument("--openpilot-longitudinal-control", action="store_true",
                        help="Expose settings that require openpilot longitudinal control")
    args = parser.parse_args()

    app = setup_aiohttp_app(
        args.host,
        args.port,
        args.debug,
        car_brand=args.car_brand,
        openpilot_longitudinal_control=args.openpilot_longitudinal_control,
    )
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
