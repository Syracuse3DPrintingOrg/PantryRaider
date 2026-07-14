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

#include <string>
#include <vector>

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

// One parse of the /cub/summary contract (docs/design: "The /cub/summary
// contract"). Every block degrades to empty/zero, mirroring the server.
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
};

enum PairingState : uint8_t {
  PAIRING_IDLE = 0,     // holding a key (from flash or YAML); not pairing
  PAIRING_NO_SERVER,    // no server address yet (discovery still looking)
  PAIRING_REQUESTING,   // POSTing /api/pairing/request (or backing off to retry)
  PAIRING_WAITING,      // code is on screen; polling /api/pairing/status
  PAIRING_DENIED,       // the user said no; waiting before asking again
  PAIRING_EXPIRED,      // the request timed out; re-requesting shortly
};

class PantryRaiderHub : public PollingComponent {
 public:
  void setup() override;
  void update() override;
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
  void start_pairing_();
  void poll_pairing_status_();
  void stop_pairing_polling_();
  void schedule_pairing_retry_(uint32_t delay_ms);
#ifdef PR_USE_DISCOVERY
  void discover_();
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

  ESPPreferenceObject key_pref_;

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
