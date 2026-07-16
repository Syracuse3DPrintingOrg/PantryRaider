"""Pantry Raider hub for ESPHome: the heart of a Bandit Cub.

The hub polls the server's ``/cub/summary``, runs the pairing flow when the
device has no key yet, and hands the parsed kitchen state to display lambdas,
LVGL pages, and the ``sensor``/``text_sensor`` platforms in this package. It
also provides the ``pantry_raider.press`` / ``timer_extend`` / ``timer_dismiss``
actions for buttons and touch targets.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import esp32_ble_tracker, http_request, time as time_
from esphome.const import CONF_ID, CONF_PORT, CONF_TIME_ID

CODEOWNERS = ["@Syracuse3DPrintingOrg"]
DEPENDENCIES = ["http_request"]
AUTO_LOAD = ["json"]

CONF_PANTRY_RAIDER_ID = "pantry_raider_id"
CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_SERVER = "server"
CONF_API_KEY = "api_key"
CONF_PROFILE = "profile"
CONF_DEVICE_NAME = "device_name"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_BUTTON = "button"
CONF_LONG = "long"
CONF_TIMER_ID = "timer_id"
CONF_SECONDS = "seconds"
CONF_TRANSPORT = "transport"
CONF_INSTALL_TAG = "install_tag"
CONF_RELAY = "relay"

# Matches the CubTransport enum in pantry_raider.h.
TRANSPORTS = {"lan": 0, "ble": 1, "auto": 2}

pantry_raider_ns = cg.esphome_ns.namespace("pantry_raider")
PantryRaiderHub = pantry_raider_ns.class_("PantryRaiderHub", cg.PollingComponent)
PressAction = pantry_raider_ns.class_("PressAction", automation.Action)
TimerExtendAction = pantry_raider_ns.class_("TimerExtendAction", automation.Action)
TimerDismissAction = pantry_raider_ns.class_("TimerDismissAction", automation.Action)

def _install_tag(value):
    """Empty, or the first 4 bytes of sha256(device_id) as 8 hex characters
    (the sender tag every broadcast packet carries). Empty means lock onto
    the first sender heard."""
    value = cv.string_strict(value)
    if value == "":
        return value
    value = value.lower()
    if len(value) != 8 or any(c not in "0123456789abcdef" for c in value):
        raise cv.Invalid(
            "install_tag must be 8 hex characters (4 bytes), e.g. 5eae1602"
        )
    return value


_BASE_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(PantryRaiderHub),
        cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(
            http_request.HttpRequestComponent
        ),
        cv.GenerateID(CONF_TIME_ID): cv.use_id(time_.RealTimeClock),
        # Empty server means auto-discover, in order: a server remembered in
        # flash from a past run, then mDNS (_pantry-raider._tcp, preferring the
        # install whose TXT mode says server or pi_hosted), then a LAN sweep for
        # a bridge-networked Docker server that cannot advertise mDNS.
        cv.Optional(CONF_SERVER, default=""): cv.string,
        cv.Optional(CONF_PORT, default=9284): cv.port,
        # A literal key skips pairing (the YAML escape hatch). Leave it empty
        # on prebuilt Cubs; the pairing flow mints and stores one.
        cv.Optional(CONF_API_KEY, default=""): cv.sensitive(cv.string),
        cv.Optional(CONF_PROFILE, default="custom"): cv.string_strict,
        # Defaults to the ESPHome node name; sent as X-Cub-Name and used as
        # the pairing hostname, which becomes the device's name on approval.
        cv.Optional(CONF_DEVICE_NAME, default=""): cv.string,
        cv.Optional(CONF_FIRMWARE_VERSION, default="0.0.0"): cv.string_strict,
        # BLE advertisement relay (FoodAssistant-nn3u): forward the kitchen
        # sensors this Cub hears to the server, which decodes them. Needs an
        # esp32_ble_tracker in the config (the schema below asks for one as
        # soon as this is on, whatever the transport is) and the server's
        # cub_ble_relay setting; off here means the code is not even built in.
        cv.Optional(CONF_RELAY, default=False): cv.boolean,
    }
).extend(cv.polling_component_schema("15s"))

# BLE receive needs an esp32_ble_tracker in the config; the schema only asks
# for one when the transport actually uses the radio, so a plain LAN Cub
# never pulls the BLE stack into its build.
_BLE_SCHEMA = _BASE_SCHEMA.extend(esp32_ble_tracker.ESP_BLE_DEVICE_SCHEMA).extend(
    {
        cv.Optional(CONF_INSTALL_TAG, default=""): _install_tag,
    }
)

# The relay needs the radio too, so a LAN Cub with the relay on wants the
# tracker in its config while a plain LAN Cub must never be asked for one.
_LAN_RELAY_SCHEMA = _BASE_SCHEMA.extend(esp32_ble_tracker.ESP_BLE_DEVICE_SCHEMA)


def _relay_on(config) -> bool:
    """Whether the relay is on, read before the schema has coerced anything.

    The value may still be the raw string a substitution produced ("false"
    reads as truthy to plain Python), so it goes through cv.boolean first."""
    if not isinstance(config, dict) or CONF_RELAY not in config:
        return False
    try:
        return bool(cv.boolean(config[CONF_RELAY]))
    except (cv.Invalid, TypeError, ValueError):
        return False  # let the real schema below report the bad value


def _lan_schema(config):
    """transport: lan. Only asks for an esp32_ble_tracker when the relay is
    on, so the default LAN Cub never pulls the BLE stack into its build."""
    if _relay_on(config):
        return _LAN_RELAY_SCHEMA(config)
    return _BASE_SCHEMA(config)

# transport picks how the Cub gets its kitchen state:
#   lan  (default): poll /cub/summary over Wi-Fi, exactly as before.
#   ble:  never pair, never poll; passively listen for the status broadcast
#         a Pi appliance (or any install with cub_ble_advertise on) sends.
#   auto: LAN while paired and the last poll succeeded, the freshest BLE
#         broadcast whenever the LAN feed is down.
CONFIG_SCHEMA = cv.typed_schema(
    {"lan": _lan_schema, "ble": _BLE_SCHEMA, "auto": _BLE_SCHEMA},
    key=CONF_TRANSPORT,
    default_type="lan",
    lower=True,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    http = await cg.get_variable(config[CONF_HTTP_REQUEST_ID])
    cg.add(var.set_http(http))
    rtc = await cg.get_variable(config[CONF_TIME_ID])
    cg.add(var.set_time(rtc))
    cg.add(var.set_server(config[CONF_SERVER]))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_api_key(config[CONF_API_KEY]))
    cg.add(var.set_profile(config[CONF_PROFILE]))
    if config[CONF_DEVICE_NAME]:
        cg.add(var.set_device_name(config[CONF_DEVICE_NAME]))
    cg.add(var.set_firmware_version(config[CONF_FIRMWARE_VERSION]))
    transport = config[CONF_TRANSPORT]
    cg.add(var.set_transport(TRANSPORTS[transport]))
    relay = config[CONF_RELAY]
    if transport != "lan" or relay:
        # Either job needs the radio and the scan callback.
        cg.add_define("PR_USE_BLE")
        await esp32_ble_tracker.register_ble_device(var, config)
    if transport != "lan":
        if config[CONF_INSTALL_TAG]:
            cg.add(var.set_install_tag(config[CONF_INSTALL_TAG]))
    if relay:
        cg.add_define("PR_USE_BLE_RELAY")
        cg.add(var.set_relay(True))
    if not config[CONF_SERVER] and transport != "ble":
        # Only pull the mDNS query code in when discovery is actually needed.
        # A BLE-only Cub never talks to a server, so it never discovers one.
        cg.add_define("PR_USE_DISCOVERY")


PRESS_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(PantryRaiderHub),
        cv.Required(CONF_BUTTON): cv.templatable(cv.string),
        cv.Optional(CONF_LONG, default=False): cv.templatable(cv.boolean),
    }
)


@automation.register_action(
    "pantry_raider.press", PressAction, PRESS_ACTION_SCHEMA, synchronous=True
)
async def press_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    button = await cg.templatable(config[CONF_BUTTON], args, cg.std_string)
    cg.add(var.set_button(button))
    long_press = await cg.templatable(config[CONF_LONG], args, bool)
    cg.add(var.set_long_press(long_press))
    return var


TIMER_EXTEND_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(PantryRaiderHub),
        cv.Required(CONF_TIMER_ID): cv.templatable(cv.string),
        cv.Optional(CONF_SECONDS, default=60): cv.templatable(cv.positive_int),
    }
)


@automation.register_action(
    "pantry_raider.timer_extend", TimerExtendAction, TIMER_EXTEND_SCHEMA,
    synchronous=True,
)
async def timer_extend_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    timer_id = await cg.templatable(config[CONF_TIMER_ID], args, cg.std_string)
    cg.add(var.set_timer_id(timer_id))
    seconds = await cg.templatable(config[CONF_SECONDS], args, cg.int_)
    cg.add(var.set_seconds(seconds))
    return var


TIMER_DISMISS_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(PantryRaiderHub),
        cv.Required(CONF_TIMER_ID): cv.templatable(cv.string),
    }
)


@automation.register_action(
    "pantry_raider.timer_dismiss", TimerDismissAction, TIMER_DISMISS_SCHEMA,
    synchronous=True,
)
async def timer_dismiss_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    timer_id = await cg.templatable(config[CONF_TIMER_ID], args, cg.std_string)
    cg.add(var.set_timer_id(timer_id))
    return var
