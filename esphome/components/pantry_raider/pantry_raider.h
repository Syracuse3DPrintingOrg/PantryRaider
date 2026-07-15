// Pantry Raider hub component: the one object a Bandit Cub runs.
//
// Owns the /cub/summary poll, the parsed kitchen state every display lambda
// and LVGL page reads, the pairing state machine (request a code, poll for
// approval, keep the minted key in flash), and the action calls a button or
// touch target fires back at the server.
#pragma once

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/core/preferences.h"
#include "esphome/components/http_request/http_request.h"
#include "esphome/components/time/real_time_clock.h"
#ifdef USE_SENSOR
#include "esphome/components/sensor/sensor.h"
#endif
#ifdef USE_TEXT_SENSOR
#include "esphome/components/text_sensor/text_sensor.h"
#endif
#ifdef PR_USE_BLE
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"
#include "cub_ble_parse.h"
#endif
#ifdef PR_USE_BLE_RELAY
// The relay reads its allowlist straight out of the parsed summary.
#include "esphome/components/json/json_util.h"
#endif

#include <string>
#include <vector>

// The LAN sweep is the last-resort way to find a server, and it needs raw
// sockets, so it only builds when discovery is on and we are on ESP-IDF.
// PR_USE_DISCOVERY and USE_ESP_IDF both arrive through core/component.h above.
#if defined(PR_USE_DISCOVERY) && defined(USE_ESP_IDF)
#define PR_USE_SWEEP
#endif

namespace esphome {
namespace pantry_raider {

struct CubTimer {
  std::string id;
  std::string label;
  int64_t deadline_epoch{0};
  bool expired{false};
};

struct CubExpiringItem {
  std::string name;
  int days{0};
};

struct CubProbe {
  std::string id;
  std::string name;
  int probe{0};
  float temp_c{NAN};
  float target_c{NAN};
  std::string direction;
  bool stale{false};
};

struct CubSettings {
  std::string default_view{"expiring"};
  bool timers_take_over{true};
  bool probes_take_over{true};
  int rotate_seconds{12};
  int poll_seconds{15};
  std::string units{"f"};
  bool clock_24h{false};
};

// How the Cub gets its kitchen state. Matches TRANSPORTS in __init__.py.
enum CubTransport : uint8_t {
  CUB_TRANSPORT_LAN = 0,   // poll /cub/summary over Wi-Fi (the default)
  CUB_TRANSPORT_BLE = 1,   // receive-only: listen for the BLE status broadcast
  CUB_TRANSPORT_AUTO = 2,  // LAN when paired and reachable, BLE fallback
};

#ifdef PR_USE_BLE_RELAY
// The advertisement relay (FoodAssistant-nn3u). This Cub is a radio for a
// server that has none: it forwards the raw advertisements of the sensors the
// server asked about and the server decodes them.
//
// Every buffer here is fixed-capacity on purpose. This runs in the scan
// callback, on a device that also drives a display, so the relay is never
// allowed to grow: a full queue drops the oldest packet and a busy kitchen
// costs exactly the same RAM as an empty one.

// Allowlist caps. The server sends what its decoders match (about 2 company
// ids, 5 service UUIDs, and 20 name prefixes today); anything past these caps
// is simply not stored, so a future server can never overrun this device.
static const uint8_t PR_RELAY_ALLOW_MAX = 12;
static const uint8_t PR_RELAY_NAMES_MAX = 24;
static const uint8_t PR_RELAY_NAME_LEN = 12;  // "tempspike" and kin all fit
// A full advertisement plus its scan response: the whole addressable space.
static const uint8_t PR_RELAY_ADV_MAX = 62;
// Packets held between flushes. The server caps a batch at 25; ten keeps the
// buffer near 700 bytes and still covers a burst.
static const uint8_t PR_RELAY_QUEUE_MAX = 10;
// Recently relayed (mac, payload) pairs, so the repeat burst a radio hears
// for one press or one reading is sent once.
static const uint8_t PR_RELAY_SEEN_MAX = 16;
static const uint32_t PR_RELAY_DEDUPE_MS = 3000;

// One advertisement waiting to be forwarded.
struct RelayPacket {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t adv[PR_RELAY_ADV_MAX];
};

// One (device, payload) pair already sent, for the dedupe window.
struct RelaySeen {
  uint8_t mac[6];
  uint32_t hash;
  uint32_t ms;
};
#endif

// One parse of the /cub/summary contract (docs/design: "The /cub/summary
// contract"). Every block degrades to empty/zero, mirroring the server.
// A BLE broadcast fills a reduced version of the same struct (counts, one
// synthesized timer, one probe), so display lambdas are transport-agnostic.
struct CubState {
  bool valid{false};  // at least one good summary has been parsed
  int64_t generated{0};
  std::string view{"expiring"};
  std::vector<std::string> rotation;
  bool expiring_ok{false};
  int expired{0};
  int today{0};
  int soon{0};
  int window_days{0};
  std::vector<CubExpiringItem> top;
  int pending{0};
  int action_items{0};
  std::vector<CubTimer> timers;
  std::vector<CubProbe> probes;
  CubSettings settings;
  // BLE broadcast only: the attention flag (a protection alarm is live on
  // the server). LAN summaries leave it false; custom lambdas may use it.
  bool attention{false};
};

enum PairingState : uint8_t {
  PAIRING_IDLE = 0,     // holding a key (from flash or YAML); not pairing
  PAIRING_NO_SERVER,    // no server address yet (discovery still looking)
  PAIRING_SEARCHING,    // sweeping the LAN for a server right now
  PAIRING_REQUESTING,   // POSTing /api/pairing/request (or backing off to retry)
  PAIRING_WAITING,      // code is on screen; polling /api/pairing/status
  PAIRING_DENIED,       // the user said no; waiting before asking again
  PAIRING_EXPIRED,      // the request timed out; re-requesting shortly
};

#ifdef PR_USE_SWEEP
// Phases of the non-blocking LAN sweep. It runs a batch of TCP connects per
// loop() so the display stays alive and the watchdog never trips.
enum SweepPhase : uint8_t {
  SWEEP_NONE = 0,   // not sweeping
  SWEEP_CONNECT,    // a batch of non-blocking connects is in flight
  SWEEP_HEALTH,     // an open host is being fingerprinted over /health
};

// One host with an open port, awaiting its /health fingerprint.
struct SweepHit {
  uint32_t ip{0};   // host byte order (converted to network order at the socket)
  uint16_t port{0};
};
#endif

class PantryRaiderHub : public PollingComponent
#ifdef PR_USE_BLE
    ,
                        public esp32_ble_tracker::ESPBTDeviceListener
#endif
{
 public:
  void setup() override;
  void update() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_CONNECTION; }

  // Config setters (from codegen).
  void set_http(http_request::HttpRequestComponent *http) { this->http_ = http; }
  void set_time(time::RealTimeClock *time) { this->time_ = time; }
  void set_server(const std::string &server) { this->server_ = server; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_api_key(const std::string &key) { this->yaml_api_key_ = key; }
  void set_profile(const std::string &profile) { this->profile_ = profile; }
  void set_device_name(const std::string &name) { this->device_name_ = name; }
  void set_firmware_version(const std::string &v) { this->version_ = v; }
  void set_transport(uint8_t t) { this->transport_ = (CubTransport) t; }
#ifdef PR_USE_BLE_RELAY
  void set_relay(bool on) { this->relay_ = on; }
  // True once the server has asked for the relay and sent an allowlist.
  bool relay_active() const { return this->relay_ && this->relay_allowed_; }
#endif
#ifdef PR_USE_BLE
  // 8 hex chars from YAML: pin the sender this Cub listens to. Empty (the
  // default) means the first sender heard wins and is remembered in flash.
  void set_install_tag(const std::string &hex);
  bool parse_device(const esp32_ble_tracker::ESPBTDevice &device) override;
#endif

#ifdef USE_SENSOR
  void set_expired_sensor(sensor::Sensor *s) { this->expired_sensor_ = s; }
  void set_today_sensor(sensor::Sensor *s) { this->today_sensor_ = s; }
  void set_soon_sensor(sensor::Sensor *s) { this->soon_sensor_ = s; }
  void set_pending_sensor(sensor::Sensor *s) { this->pending_sensor_ = s; }
  void set_active_timers_sensor(sensor::Sensor *s) { this->active_timers_sensor_ = s; }
  void set_next_timer_seconds_sensor(sensor::Sensor *s) { this->next_timer_seconds_sensor_ = s; }
  void set_probe_temperature_sensor(sensor::Sensor *s) { this->probe_temperature_sensor_ = s; }
#endif
#ifdef USE_TEXT_SENSOR
  void set_view_text_sensor(text_sensor::TextSensor *s) { this->view_text_sensor_ = s; }
  void set_next_timer_text_sensor(text_sensor::TextSensor *s) { this->next_timer_text_sensor_ = s; }
  void set_pairing_code_text_sensor(text_sensor::TextSensor *s) { this->pairing_code_text_sensor_ = s; }
#endif

  // --- State access for display lambdas and LVGL pages ---
  const CubState &state() const { return this->state_; }
  bool online() const { return this->online_; }
  bool paired() const { return !this->api_key_.empty(); }
  PairingState pairing_state() const { return this->pairing_state_; }
  const std::string &pairing_code() const { return this->pairing_code_; }
  const std::string &cub_id() const { return this->cub_id_; }
  const std::string &server() const { return this->server_; }
  CubTransport transport() const { return this->transport_; }
#ifdef PR_USE_BLE
  // A broadcast packet was applied recently enough to trust on screen.
  bool ble_fresh() const;
  // The current state came from a BLE broadcast (vs a LAN summary).
  bool state_from_ble() const { return this->state_from_ble_; }
#endif

  // Epoch "now": SNTP when synced, else the last summary's generated stamp
  // advanced by local uptime, so countdowns still tick before the first sync.
  int64_t now_epoch() const;
  // Seconds left on a timer (negative once past the deadline).
  int timer_remaining(const CubTimer &t) const;
  // Index of the timer to feature: a ringing one first, else the soonest
  // running one. -1 when there are no timers.
  int featured_timer() const;
  // "M:SS" / "H:MM:SS", or "DONE" for an expired timer.
  std::string format_remaining(const CubTimer &t) const;
  // The view a display should render right now: the server's decision, or
  // "pairing" / "offline" when the Cub is not connected yet.
  std::string effective_view() const;
  // Human line for the pairing screen ("Pair me: code 1234", etc.).
  std::string pairing_line() const;
  // Probe temperature in the server's configured display units.
  float probe_display_temp(const CubProbe &p) const;
  const char *units_suffix() const { return this->state_.settings.units == "c" ? "°C" : "°F"; }
  // Wall-clock text ("7:42 PM" or "19:42") honoring the server's 24h setting.
  std::string clock_text() const;

  // --- Actions (also exposed as pantry_raider.* YAML actions) ---
  void press(const std::string &button, bool long_press = false);
  void timer_extend(const std::string &timer_id, int seconds = 60);
  void timer_dismiss(const std::string &timer_id);
  void refresh() { this->update(); }

 protected:
  std::string base_url_() const;
  std::vector<http_request::Header> headers_(bool with_json, bool authed) const;
  // One HTTP round trip; fills body_in (capped), returns the status code or -1.
  int fetch_(const std::string &method, const std::string &path, const std::string &body_out,
             std::string &body_in, bool authed = true);
  void poll_summary_();
  bool parse_summary_(const std::string &body);
  void publish_();
  void load_key_();
  void save_key_(const std::string &key);
#ifdef PR_USE_BLE_RELAY
  // Read the summary's ble_relay block into the allowlist (and turn the relay
  // off again when the block is gone: the server had second thoughts).
  void relay_read_allowlist_(JsonObject root);
  // Does this advertisement match the allowlist the server sent?
  bool relay_wanted_(const esp32_ble_tracker::ESPBTDevice &device) const;
  // Queue one advertisement, unless it is a repeat inside the dedupe window.
  void relay_capture_(const esp32_ble_tracker::ESPBTDevice &device);
  // POST whatever is queued. Drops the batch when there is nothing to send it
  // to (unpaired, or no server yet): the relay is best-effort by design.
  void relay_flush_();
#endif
#ifdef PR_USE_BLE
  void apply_ble_packet_(const CubBlePacket &pkt);
  void load_tag_();
  void save_tag_();
#endif
  void start_pairing_();
  void poll_pairing_status_();
  void stop_pairing_polling_();
  void schedule_pairing_retry_(uint32_t delay_ms);
#ifdef PR_USE_DISCOVERY
  void discover_();
#endif
#ifdef PR_USE_SWEEP
  // Kick off a LAN sweep (fills sweep_* and flips pairing to SEARCHING). Safe
  // to call when one is already running or the backoff has not elapsed: it
  // simply does nothing.
  void start_sweep_();
  // Run one batch of the sweep. Called from loop() so the display and the
  // watchdog both stay happy; each call does at most one connect batch or one
  // /health fingerprint.
  void step_sweep_();
  void finish_sweep_();
  // A raw, short-timeout /health GET used only during a sweep, so we never
  // block on the shared http_request 10s timeout. Fills mode/app on success.
  bool health_probe_(uint32_t ip_net, uint16_t port, std::string &mode_out);
  // Adopt a discovered server: set server_, remember it in NVS, advance state.
  void adopt_server_(const std::string &host, uint16_t port);
  void load_server_();
  void save_server_(const std::string &host, uint16_t port);
#endif

  http_request::HttpRequestComponent *http_{nullptr};
  time::RealTimeClock *time_{nullptr};

  std::string server_;
  uint16_t port_{9284};
  std::string yaml_api_key_;
  std::string api_key_;
  std::string profile_{"custom"};
  std::string device_name_;
  std::string version_{"0.0.0"};
  std::string cub_id_;

  CubState state_;
  bool online_{false};
  uint32_t state_millis_{0};  // millis() when state_.generated was parsed
  int auth_failures_{0};

  PairingState pairing_state_{PAIRING_IDLE};
  std::string pairing_request_id_;
  std::string pairing_code_;
  bool pairing_poll_scheduled_{false};
  uint32_t last_discovery_ms_{0};
  bool server_is_explicit_{false};  // pr_server was set in YAML: never sweep/forget it
  int poll_failures_{0};            // consecutive failed summary polls (re-sweep trigger)

  ESPPreferenceObject key_pref_;

  CubTransport transport_{CUB_TRANSPORT_LAN};
#ifdef PR_USE_BLE_RELAY
  bool relay_{false};          // the YAML option: the code is built in at all
  bool relay_allowed_{false};  // the server sent an allowlist: actually relay
  uint16_t relay_company_[PR_RELAY_ALLOW_MAX]{};
  uint8_t relay_company_n_{0};
  uint16_t relay_uuid_[PR_RELAY_ALLOW_MAX]{};
  uint8_t relay_uuid_n_{0};
  char relay_names_[PR_RELAY_NAMES_MAX][PR_RELAY_NAME_LEN]{};
  uint8_t relay_names_n_{0};
  RelayPacket relay_queue_[PR_RELAY_QUEUE_MAX]{};
  uint8_t relay_queue_n_{0};
  uint8_t relay_batch_max_{PR_RELAY_QUEUE_MAX};  // server-tunable, capped here
  uint32_t relay_batch_ms_{2000};
  uint32_t relay_first_ms_{0};   // millis() the current batch started filling
  RelaySeen relay_seen_[PR_RELAY_SEEN_MAX]{};
  uint8_t relay_seen_i_{0};      // next slot in the ring
#endif
#ifdef PR_USE_BLE
  ESPPreferenceObject tag_pref_;
  uint8_t ble_tag_[4]{0, 0, 0, 0};
  bool ble_tag_set_{false};     // a tag is known (YAML, flash, or first-heard)
  bool ble_tag_pinned_{false};  // the tag came from YAML; never overwritten
  uint32_t ble_last_ms_{0};     // millis() of the last accepted packet (0 never)
  int ble_applied_seq_{-1};     // last sequence rendered; dedupes repaints
  bool state_from_ble_{false};  // state_ was filled from a broadcast
#endif

#ifdef PR_USE_SWEEP
  // One Pantry Raider instance the sweep confirmed over /health.
  struct SweepCandidate {
    uint32_t ip{0};   // host byte order, for easy lowest-IP tie-breaking
    uint16_t port{0};
    uint8_t rank{0};  // 0 server/pi_hosted, 1 empty/unknown, 2 pi_remote
  };

  ESPPreferenceObject server_pref_;
  SweepPhase sweep_phase_{SWEEP_NONE};
  uint32_t sweep_net_{0};       // host-order base of the /24 (last octet zeroed)
  uint32_t sweep_self_{0};      // our own host-order address, skipped in the sweep
  uint16_t sweep_port_{9284};   // port this pass probes (9284 first, then 80)
  uint16_t sweep_idx_{1};       // next host octet (1..254) to probe
  std::vector<bool> sweep_opened_;      // per-host: already found open this sweep
  std::vector<SweepHit> sweep_hits_;    // open host:port pairs awaiting /health
  std::vector<SweepCandidate> sweep_cands_;  // confirmed instances so far
  uint32_t last_sweep_ms_{0};   // millis() of the last sweep start (backoff)
  bool sweep_ever_ran_{false};  // gates the backoff on the very first sweep
#endif

#ifdef USE_SENSOR
  sensor::Sensor *expired_sensor_{nullptr};
  sensor::Sensor *today_sensor_{nullptr};
  sensor::Sensor *soon_sensor_{nullptr};
  sensor::Sensor *pending_sensor_{nullptr};
  sensor::Sensor *active_timers_sensor_{nullptr};
  sensor::Sensor *next_timer_seconds_sensor_{nullptr};
  sensor::Sensor *probe_temperature_sensor_{nullptr};
#endif
#ifdef USE_TEXT_SENSOR
  text_sensor::TextSensor *view_text_sensor_{nullptr};
  text_sensor::TextSensor *next_timer_text_sensor_{nullptr};
  text_sensor::TextSensor *pairing_code_text_sensor_{nullptr};
#endif
};

}  // namespace pantry_raider
}  // namespace esphome
