#include "pantry_raider.h"

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/components/json/json_util.h"

#include <cinttypes>
#include <cstdio>

#ifdef PR_USE_DISCOVERY
#ifdef USE_ESP_IDF
#include <mdns.h>
#endif
#ifdef USE_ARDUINO
#include <ESPmDNS.h>
#endif
#endif

namespace esphome {
namespace pantry_raider {

static const char *const TAG = "pantry_raider";

// The stored key: token_urlsafe(32) is 43 chars; leave headroom.
struct KeyStore {
  char key[80];
};

static const uint32_t KEY_PREF_HASH = 0x50524B31;  // "PRK1"
static const size_t BODY_CAP = 16384;
static const uint32_t PAIR_POLL_MS = 3000;
static const uint32_t PAIR_RETRY_DENIED_MS = 120000;
static const uint32_t PAIR_RETRY_EXPIRED_MS = 15000;
static const uint32_t PAIR_RETRY_ERROR_MS = 30000;
static const uint32_t DISCOVERY_EVERY_MS = 20000;

void PantryRaiderHub::setup() {
  // MAC-derived cub id, e.g. cub-a4cf12 (last three octets).
  std::string mac = get_mac_address();  // 12 lowercase hex chars
  this->cub_id_ = "cub-" + (mac.size() >= 6 ? mac.substr(mac.size() - 6) : mac);
  if (this->device_name_.empty())
    this->device_name_ = App.get_name();

  this->key_pref_ = global_preferences->make_preference<KeyStore>(KEY_PREF_HASH);
  this->load_key_();
  if (!this->yaml_api_key_.empty()) {
    // A literal key in YAML wins and skips pairing entirely (the escape hatch).
    this->api_key_ = this->yaml_api_key_;
  }
  if (this->server_.empty())
    this->pairing_state_ = PAIRING_NO_SERVER;
}

void PantryRaiderHub::dump_config() {
  ESP_LOGCONFIG(TAG, "Pantry Raider hub:");
  ESP_LOGCONFIG(TAG, "  Cub id: %s", this->cub_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Profile: %s", this->profile_.c_str());
  ESP_LOGCONFIG(TAG, "  Version: %s", this->version_.c_str());
  if (this->server_.empty()) {
    ESP_LOGCONFIG(TAG, "  Server: (mDNS discovery)");
  } else {
    ESP_LOGCONFIG(TAG, "  Server: %s:%u", this->server_.c_str(), this->port_);
  }
  ESP_LOGCONFIG(TAG, "  Paired: %s", this->paired() ? "yes" : "no");
}

void PantryRaiderHub::update() {
  if (this->server_.empty()) {
#ifdef PR_USE_DISCOVERY
    this->discover_();
#endif
    if (this->server_.empty()) {
      this->pairing_state_ = PAIRING_NO_SERVER;
      this->publish_();
      return;
    }
  }
  if (this->api_key_.empty()) {
    if (this->pairing_state_ == PAIRING_IDLE || this->pairing_state_ == PAIRING_NO_SERVER)
      this->start_pairing_();
    this->publish_();
    return;
  }
  this->poll_summary_();
}

// --- HTTP plumbing -----------------------------------------------------------

std::string PantryRaiderHub::base_url_() const {
  // The server config may be "host", "host:port", or a full http:// URL.
  std::string s = this->server_;
  if (s.compare(0, 7, "http://") == 0 || s.compare(0, 8, "https://") == 0) {
    while (!s.empty() && s.back() == '/')
      s.pop_back();
    return s;
  }
  if (s.find(':') == std::string::npos)
    s += ":" + to_string(this->port_);
  return "http://" + s;
}

std::vector<http_request::Header> PantryRaiderHub::headers_(bool with_json, bool authed) const {
  std::vector<http_request::Header> headers;
  if (authed && !this->api_key_.empty())
    headers.push_back({"X-API-Key", this->api_key_});
  headers.push_back({"X-Cub-Id", this->cub_id_});
  headers.push_back({"X-Cub-Profile", this->profile_});
  headers.push_back({"X-Cub-Version", this->version_});
  headers.push_back({"X-Cub-Name", this->device_name_});
  if (with_json)
    headers.push_back({"Content-Type", "application/json"});
  return headers;
}

int PantryRaiderHub::fetch_(const std::string &method, const std::string &path, const std::string &body_out,
                            std::string &body_in, bool authed) {
  body_in.clear();
  if (this->http_ == nullptr)
    return -1;
  std::string url = this->base_url_() + path;
  auto container = this->http_->start(url, method, body_out, this->headers_(!body_out.empty(), authed));
  if (container == nullptr) {
    ESP_LOGW(TAG, "%s %s failed (no response)", method.c_str(), url.c_str());
    return -1;
  }
  int status = container->status_code;
  size_t len = container->content_length;
  if (len > 0) {
    size_t want = std::min(len, BODY_CAP);
    body_in.resize(want);
    auto result = http_request::http_read_fully(container.get(), reinterpret_cast<uint8_t *>(&body_in[0]), want, 512,
                                                this->http_->get_timeout());
    if (result.status != http_request::HttpReadStatus::OK)
      body_in.resize(container->get_bytes_read() <= want ? container->get_bytes_read() : want);
  }
  container->end();
  return status;
}

// --- Summary poll ------------------------------------------------------------

void PantryRaiderHub::poll_summary_() {
  std::string body;
  int status = this->fetch_("GET", "/cub/summary", "", body);
  if (status == 200) {
    this->auth_failures_ = 0;
    if (this->parse_summary_(body)) {
      this->online_ = true;
      // The server's poll_seconds wins over the YAML interval.
      uint32_t want = (uint32_t) this->state_.settings.poll_seconds * 1000;
      if (want >= 5000 && want != this->get_update_interval()) {
        ESP_LOGI(TAG, "Server set poll interval to %us", this->state_.settings.poll_seconds);
        this->set_update_interval(want);
        this->stop_poller();
        this->start_poller();
      }
    }
  } else if (status == 401 || status == 403) {
    this->online_ = false;
    this->auth_failures_++;
    ESP_LOGW(TAG, "Summary poll rejected (%d), failure %d", status, this->auth_failures_);
    if (this->auth_failures_ >= 3 && this->yaml_api_key_.empty()) {
      // The key was revoked (or never worked): forget it and re-pair.
      ESP_LOGW(TAG, "Dropping stored key and re-pairing");
      this->api_key_.clear();
      this->save_key_("");
      this->start_pairing_();
    }
  } else {
    this->online_ = false;
    ESP_LOGW(TAG, "Summary poll failed (status %d)", status);
  }
  this->publish_();
}

bool PantryRaiderHub::parse_summary_(const std::string &body) {
  bool ok = json::parse_json(body, [this](JsonObject root) -> bool {
    CubState st;
    st.valid = true;
    st.generated = root["generated"] | (int64_t) 0;
    st.view = std::string(root["view"] | "expiring");
    for (JsonVariant v : root["rotation"].as<JsonArray>())
      st.rotation.emplace_back(v.as<const char *>() ? v.as<const char *>() : "");
    JsonObject exp = root["expiring"];
    if (!exp.isNull()) {
      st.expiring_ok = exp["ok"] | false;
      st.expired = exp["expired"] | 0;
      st.today = exp["today"] | 0;
      st.soon = exp["soon"] | 0;
      st.window_days = exp["window_days"] | 0;
      for (JsonObject item : exp["top"].as<JsonArray>()) {
        CubExpiringItem e;
        e.name = std::string(item["name"] | "");
        e.days = item["days"] | 0;
        st.top.push_back(e);
      }
    }
    JsonObject counts = root["counts"];
    if (!counts.isNull()) {
      st.pending = counts["pending"] | 0;
      st.action_items = counts["action_items"] | 0;
    }
    for (JsonObject t : root["timers"].as<JsonArray>()) {
      CubTimer timer;
      // Timer ids come back as ints from the live server and strings in the
      // design examples; carry them as text either way.
      JsonVariant idv = t["id"];
      if (idv.is<const char *>()) {
        timer.id = std::string(idv.as<const char *>());
      } else {
        timer.id = to_string(idv.as<long long>());
      }
      timer.label = std::string(t["label"] | "Timer");
      timer.deadline_epoch = t["deadline_epoch"] | (int64_t) 0;
      timer.expired = t["expired"] | false;
      st.timers.push_back(timer);
    }
    for (JsonObject p : root["probes"].as<JsonArray>()) {
      CubProbe probe;
      probe.id = std::string(p["id"] | "");
      probe.name = std::string(p["name"] | "Probe");
      probe.probe = p["probe"] | 0;
      probe.temp_c = p["temp_c"] | NAN;
      probe.target_c = p["target_c"] | NAN;
      probe.direction = std::string(p["direction"] | "");
      probe.stale = p["stale"] | false;
      st.probes.push_back(probe);
    }
    JsonObject settings = root["settings"];
    if (!settings.isNull()) {
      st.settings.default_view = std::string(settings["default_view"] | "expiring");
      st.settings.timers_take_over = settings["timers_take_over"] | true;
      st.settings.probes_take_over = settings["probes_take_over"] | true;
      st.settings.rotate_seconds = settings["rotate_seconds"] | 12;
      st.settings.poll_seconds = settings["poll_seconds"] | 15;
      st.settings.units = std::string(settings["units"] | "f");
      st.settings.clock_24h = settings["clock_24h"] | false;
    }
    this->state_ = std::move(st);
    this->state_millis_ = millis();
    return true;
  });
  if (!ok)
    ESP_LOGW(TAG, "Could not parse /cub/summary reply");
  return ok;
}

// --- Pairing state machine ---------------------------------------------------

void PantryRaiderHub::load_key_() {
  KeyStore stored{};
  if (this->key_pref_.load(&stored)) {
    stored.key[sizeof(stored.key) - 1] = '\0';
    this->api_key_ = stored.key;
    if (!this->api_key_.empty())
      ESP_LOGI(TAG, "Loaded stored API key");
  }
}

void PantryRaiderHub::save_key_(const std::string &key) {
  KeyStore stored{};
  strncpy(stored.key, key.c_str(), sizeof(stored.key) - 1);
  this->key_pref_.save(&stored);
  global_preferences->sync();
}

void PantryRaiderHub::start_pairing_() {
  if (this->pairing_state_ == PAIRING_WAITING)
    return;  // already showing a code and polling
  std::string body;
  std::string request = json::build_json([this](JsonObject root) {
                          root["hostname"] = this->device_name_;
                        }).c_str();
  int status = this->fetch_("POST", "/api/pairing/request", request, body, false);
  if (status != 200) {
    // Pairing off, not LAN, queue full (429), or the server is unreachable:
    // back off and ask again.
    ESP_LOGW(TAG, "Pairing request failed (status %d); retrying", status);
    this->pairing_state_ = PAIRING_REQUESTING;
    this->schedule_pairing_retry_(PAIR_RETRY_ERROR_MS);
    this->publish_();
    return;
  }
  bool ok = json::parse_json(body, [this](JsonObject root) -> bool {
    if (!(root["ok"] | false))
      return false;
    this->pairing_request_id_ = std::string(root["request_id"] | "");
    this->pairing_code_ = std::string(root["code"] | "");
    return !this->pairing_request_id_.empty();
  });
  if (!ok) {
    this->pairing_state_ = PAIRING_REQUESTING;
    this->schedule_pairing_retry_(PAIR_RETRY_ERROR_MS);
    this->publish_();
    return;
  }
  ESP_LOGI(TAG, "Pairing requested; code %s is on our screen, approve it in Settings, Devices",
           this->pairing_code_.c_str());
  this->pairing_state_ = PAIRING_WAITING;
  if (!this->pairing_poll_scheduled_) {
    this->pairing_poll_scheduled_ = true;
    this->set_interval("pr_pair_poll", PAIR_POLL_MS, [this]() { this->poll_pairing_status_(); });
  }
  this->publish_();
}

void PantryRaiderHub::stop_pairing_polling_() {
  if (this->pairing_poll_scheduled_) {
    this->cancel_interval("pr_pair_poll");
    this->pairing_poll_scheduled_ = false;
  }
}

void PantryRaiderHub::schedule_pairing_retry_(uint32_t delay_ms) {
  this->set_timeout("pr_pair_retry", delay_ms, [this]() {
    if (this->api_key_.empty())
      this->start_pairing_();
  });
}

void PantryRaiderHub::poll_pairing_status_() {
  if (this->pairing_state_ != PAIRING_WAITING) {
    this->stop_pairing_polling_();
    return;
  }
  std::string body;
  int status = this->fetch_("GET", "/api/pairing/status/" + this->pairing_request_id_, "", body, false);
  if (status != 200)
    return;  // transient; the interval tries again in a few seconds
  std::string result;
  std::string key;
  json::parse_json(body, [&result, &key](JsonObject root) -> bool {
    result = std::string(root["status"] | "");
    key = std::string(root["api_key"] | "");
    return true;
  });
  if (result == "approved" && !key.empty()) {
    ESP_LOGI(TAG, "Pairing approved; key stored");
    this->stop_pairing_polling_();
    this->api_key_ = key;
    this->save_key_(key);
    this->pairing_state_ = PAIRING_IDLE;
    this->pairing_code_.clear();
    this->pairing_request_id_.clear();
    this->publish_();
    // Show content within a poll interval: fetch right away.
    this->set_timeout("pr_first_poll", 500, [this]() { this->update(); });
  } else if (result == "denied") {
    ESP_LOGW(TAG, "Pairing denied; will ask again later");
    this->stop_pairing_polling_();
    this->pairing_state_ = PAIRING_DENIED;
    this->pairing_code_.clear();
    this->schedule_pairing_retry_(PAIR_RETRY_DENIED_MS);
    this->publish_();
  } else if (result == "expired") {
    ESP_LOGW(TAG, "Pairing request expired; re-requesting");
    this->stop_pairing_polling_();
    this->pairing_state_ = PAIRING_EXPIRED;
    this->pairing_code_.clear();
    this->schedule_pairing_retry_(PAIR_RETRY_EXPIRED_MS);
    this->publish_();
  }
  // "pending": keep polling.
}

// --- Discovery ---------------------------------------------------------------

#ifdef PR_USE_DISCOVERY
void PantryRaiderHub::discover_() {
  uint32_t now = millis();
  if (this->last_discovery_ms_ != 0 && now - this->last_discovery_ms_ < DISCOVERY_EVERY_MS)
    return;
  this->last_discovery_ms_ = now;
  ESP_LOGD(TAG, "Browsing mDNS for _pantry-raider._tcp");
#ifdef USE_ESP_IDF
  mdns_result_t *results = nullptr;
  esp_err_t err = mdns_query_ptr("_pantry-raider", "_tcp", 2000, 8, &results);
  if (err != ESP_OK || results == nullptr)
    return;
  std::string best;
  bool best_is_server = false;
  for (mdns_result_t *r = results; r != nullptr; r = r->next) {
    if (r->addr == nullptr)
      continue;
    std::string mode;
    for (size_t i = 0; i < r->txt_count; i++) {
      if (r->txt[i].key != nullptr && strcmp(r->txt[i].key, "mode") == 0 && r->txt[i].value != nullptr)
        mode = r->txt[i].value;
    }
    // Prefer a main install; a satellite (pi_remote) owns no keys to hand out.
    bool is_server = (mode == "server" || mode == "pi_hosted");
    if (!best.empty() && (best_is_server || !is_server))
      continue;
    for (mdns_ip_addr_t *a = r->addr; a != nullptr; a = a->next) {
      if (a->addr.type != ESP_IPADDR_TYPE_V4)
        continue;
      uint32_t ip = a->addr.u_addr.ip4.addr;
      char buf[32];
      snprintf(buf, sizeof(buf), "%u.%u.%u.%u:%u", (unsigned) (ip & 0xFF), (unsigned) ((ip >> 8) & 0xFF),
               (unsigned) ((ip >> 16) & 0xFF), (unsigned) ((ip >> 24) & 0xFF), r->port);
      best = buf;
      best_is_server = is_server;
      break;
    }
  }
  mdns_query_results_free(results);
  if (!best.empty()) {
    ESP_LOGI(TAG, "Discovered Pantry Raider at %s", best.c_str());
    this->server_ = best;
    if (this->pairing_state_ == PAIRING_NO_SERVER)
      this->pairing_state_ = this->api_key_.empty() ? PAIRING_REQUESTING : PAIRING_IDLE;
  }
#endif
#ifdef USE_ARDUINO
  int n = MDNS.queryService("pantry-raider", "tcp");
  int best = -1;
  for (int i = 0; i < n; i++) {
    String mode = MDNS.txt(i, "mode");
    if (mode == "server" || mode == "pi_hosted") {
      best = i;
      break;
    }
    if (best < 0)
      best = i;
  }
  if (best >= 0) {
    std::string found = std::string(MDNS.IP(best).toString().c_str()) + ":" + to_string(MDNS.port(best));
    ESP_LOGI(TAG, "Discovered Pantry Raider at %s", found.c_str());
    this->server_ = found;
    if (this->pairing_state_ == PAIRING_NO_SERVER)
      this->pairing_state_ = this->api_key_.empty() ? PAIRING_REQUESTING : PAIRING_IDLE;
  }
#endif
}
#endif  // PR_USE_DISCOVERY

// --- Actions -----------------------------------------------------------------

void PantryRaiderHub::press(const std::string &button, bool long_press) {
  if (this->api_key_.empty() || this->server_.empty()) {
    ESP_LOGW(TAG, "press(%s) ignored: not paired yet", button.c_str());
    return;
  }
  std::string request = json::build_json([&button, long_press](JsonObject root) {
                          root["button"] = button;
                          root["long"] = long_press;
                        }).c_str();
  std::string body;
  int status = this->fetch_("POST", "/gadgets/esp-action", request, body);
  if (status != 200)
    ESP_LOGW(TAG, "esp-action %s failed (status %d)", button.c_str(), status);
  // Pick the resulting state up quickly (a new timer, a dismissed one).
  this->set_timeout("pr_refresh", 700, [this]() { this->update(); });
}

void PantryRaiderHub::timer_extend(const std::string &timer_id, int seconds) {
  if (this->api_key_.empty() || this->server_.empty() || timer_id.empty())
    return;
  std::string request = json::build_json([seconds](JsonObject root) { root["seconds"] = seconds; }).c_str();
  std::string body;
  int status = this->fetch_("POST", "/timers/" + timer_id + "/extend", request, body);
  if (status != 200)
    ESP_LOGW(TAG, "timer extend %s failed (status %d)", timer_id.c_str(), status);
  this->set_timeout("pr_refresh", 700, [this]() { this->update(); });
}

void PantryRaiderHub::timer_dismiss(const std::string &timer_id) {
  if (this->api_key_.empty() || this->server_.empty() || timer_id.empty())
    return;
  std::string body;
  int status = this->fetch_("DELETE", "/timers/" + timer_id, "", body);
  if (status != 200)
    ESP_LOGW(TAG, "timer dismiss %s failed (status %d)", timer_id.c_str(), status);
  this->set_timeout("pr_refresh", 700, [this]() { this->update(); });
}

// --- Derived state for displays ----------------------------------------------

int64_t PantryRaiderHub::now_epoch() const {
  if (this->time_ != nullptr) {
    auto now = this->time_->now();
    if (now.is_valid())
      return (int64_t) now.timestamp;
  }
  if (this->state_.generated > 0)
    return this->state_.generated + (int64_t) ((millis() - this->state_millis_) / 1000);
  return 0;
}

int PantryRaiderHub::timer_remaining(const CubTimer &t) const {
  int64_t now = this->now_epoch();
  if (now == 0)
    return 0;
  return (int) (t.deadline_epoch - now);
}

int PantryRaiderHub::featured_timer() const {
  int best = -1;
  int best_remaining = INT32_MAX;
  for (size_t i = 0; i < this->state_.timers.size(); i++) {
    const auto &t = this->state_.timers[i];
    if (t.expired)
      return (int) i;  // a ringing timer always wins
    int rem = this->timer_remaining(t);
    if (rem < best_remaining) {
      best_remaining = rem;
      best = (int) i;
    }
  }
  return best;
}

std::string PantryRaiderHub::format_remaining(const CubTimer &t) const {
  if (t.expired)
    return "DONE";
  int rem = this->timer_remaining(t);
  if (rem < 0)
    rem = 0;
  char buf[16];
  if (rem >= 3600) {
    snprintf(buf, sizeof(buf), "%d:%02d:%02d", rem / 3600, (rem % 3600) / 60, rem % 60);
  } else {
    snprintf(buf, sizeof(buf), "%d:%02d", rem / 60, rem % 60);
  }
  return buf;
}

std::string PantryRaiderHub::effective_view() const {
  if (this->api_key_.empty())
    return "pairing";
  if (!this->state_.valid)
    return this->online_ ? "clock" : "offline";
  return this->state_.view;
}

std::string PantryRaiderHub::pairing_line() const {
  switch (this->pairing_state_) {
    case PAIRING_NO_SERVER:
      return "Looking for Pantry Raider...";
    case PAIRING_REQUESTING:
      return "Asking to join...";
    case PAIRING_WAITING:
      return "Pair me: code " + this->pairing_code_;
    case PAIRING_DENIED:
      return "Pairing was denied";
    case PAIRING_EXPIRED:
      return "Pairing timed out, retrying";
    case PAIRING_IDLE:
    default:
      return "";
  }
}

float PantryRaiderHub::probe_display_temp(const CubProbe &p) const {
  if (std::isnan(p.temp_c))
    return NAN;
  if (this->state_.settings.units == "c")
    return p.temp_c;
  return p.temp_c * 9.0f / 5.0f + 32.0f;
}

std::string PantryRaiderHub::clock_text() const {
  if (this->time_ == nullptr)
    return "";
  auto now = this->time_->now();
  if (!now.is_valid())
    return "--:--";
  if (this->state_.settings.clock_24h)
    return now.strftime("%H:%M");
  std::string out = now.strftime("%I:%M %p");
  if (!out.empty() && out[0] == '0')
    out.erase(0, 1);
  return out;
}

// --- Entity publishing --------------------------------------------------------

void PantryRaiderHub::publish_() {
#ifdef USE_SENSOR
  if (this->expired_sensor_ != nullptr)
    this->expired_sensor_->publish_state(this->state_.expired);
  if (this->today_sensor_ != nullptr)
    this->today_sensor_->publish_state(this->state_.today);
  if (this->soon_sensor_ != nullptr)
    this->soon_sensor_->publish_state(this->state_.soon);
  if (this->pending_sensor_ != nullptr)
    this->pending_sensor_->publish_state(this->state_.pending);
  if (this->active_timers_sensor_ != nullptr)
    this->active_timers_sensor_->publish_state(this->state_.timers.size());
  int featured = this->featured_timer();
  if (this->next_timer_seconds_sensor_ != nullptr) {
    if (featured >= 0) {
      int rem = this->timer_remaining(this->state_.timers[featured]);
      this->next_timer_seconds_sensor_->publish_state(rem > 0 ? rem : 0);
    } else {
      this->next_timer_seconds_sensor_->publish_state(NAN);
    }
  }
  if (this->probe_temperature_sensor_ != nullptr) {
    if (!this->state_.probes.empty()) {
      this->probe_temperature_sensor_->publish_state(this->state_.probes[0].temp_c);
    } else {
      this->probe_temperature_sensor_->publish_state(NAN);
    }
  }
#endif
#ifdef USE_TEXT_SENSOR
  if (this->view_text_sensor_ != nullptr)
    this->view_text_sensor_->publish_state(this->effective_view());
  if (this->next_timer_text_sensor_ != nullptr) {
    int idx = this->featured_timer();
    if (idx >= 0) {
      const auto &t = this->state_.timers[idx];
      this->next_timer_text_sensor_->publish_state(t.label + " " + this->format_remaining(t));
    } else {
      this->next_timer_text_sensor_->publish_state("");
    }
  }
  if (this->pairing_code_text_sensor_ != nullptr)
    this->pairing_code_text_sensor_->publish_state(this->pairing_state_ == PAIRING_WAITING ? this->pairing_code_
                                                                                           : "");
#endif
}

}  // namespace pantry_raider
}  // namespace esphome
