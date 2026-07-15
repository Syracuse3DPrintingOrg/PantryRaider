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
from esphome.components import http_request, time as time_
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

pantry_raider_ns = cg.esphome_ns.namespace("pantry_raider")
PantryRaiderHub = pantry_raider_ns.class_("PantryRaiderHub", cg.PollingComponent)
PressAction = pantry_raider_ns.class_("PressAction", automation.Action)
TimerExtendAction = pantry_raider_ns.class_("TimerExtendAction", automation.Action)
TimerDismissAction = pantry_raider_ns.class_("TimerDismissAction", automation.Action)

CONFIG_SCHEMA = cv.Schema(
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
    }
).extend(cv.polling_component_schema("15s"))


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
    if not config[CONF_SERVER]:
        # Only pull the mDNS query code in when discovery is actually needed.
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
