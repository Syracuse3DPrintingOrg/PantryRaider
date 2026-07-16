#include "pantry_raider.h"

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/components/json/json_util.h"

#include <cctype>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#ifdef PR_USE_DISCOVERY
#ifdef USE_ESP_IDF
#include <mdns.h>
#endif
#ifdef USE_ARDUINO
#include <ESPmDNS.h>
#endif
#endif

#ifdef PR_USE_SWEEP
#include <esp_netif.h>
#include <lwip/sockets.h>
#include <fcntl.h>
#include <cstring>
#include <cerrno>
#endif

namespace esphome {
namespace pantry_raider {

static const char *const TAG = "pantry_raider";

// The stored key: token_urlsafe(32) is 43 chars; leave headroom.
struct KeyStore {
  char key[80];
};

static const uint32_t KEY_PREF_HASH = 0x50524B31;  // "PRK1"

#ifdef PR_USE_BLE
// A broadcast lands every second or two while the sender is alive; after
// this long without one the on-screen state is treated as gone, mirroring
// the LAN "Server unreachable" behavior.
static const uint32_t BLE_STALE_MS = 90000;
static const uint32_t TAG_PREF_HASH = 0x50525431;  // "PRT1"

// The remembered sender identity (first 4 bytes of sha256(device_id)).
struct TagStore {
  uint8_t tag[4];
  uint8_t set;
};
#endif
static const size_t BODY_CAP = 16384;
static const uint32_t PAIR_POLL_MS = 3000;
static const uint32_t PAIR_RETRY_DENIED_MS = 120000;
static const uint32_t PAIR_RETRY_EXPIRED_MS = 15000;
static const uint32_t PAIR_RETRY_ERROR_MS = 30000;
static const uint32_t DISCOVERY_EVERY_MS = 20000;

#ifdef PR_USE_SWEEP
// LWIP on the Cub caps open sockets low (CONFIG_LWIP_MAX_SOCKETS is 12), and
// the app already spends some on the poll, mDNS, and the API. Eight connects
// in flight per batch leaves comfortable headroom and still finishes a /24 in
// seconds.
static const int SWEEP_BATCH = 8;
// How long a batch of non-blocking connects waits for any of them to complete.
// Absent hosts (the common case) time out here, so this bounds each loop() step.
static const uint32_t SWEEP_CONNECT_MS = 250;
// A confirmed-open host answers /health fast; cap the whole exchange short so a
// stalled host cannot hang the sweep.
static const uint32_t SWEEP_HEALTH_MS = 500;
// Ports probed, in order of preference: the app's own port, then 80 for a
// reverse-proxied install.
static const uint16_t SWEEP_PORTS[] = {9284, 80};
// Re-sweep only after this many consecutive failed polls, and no more than
// once every few minutes, so a server that changed IP self-heals without the
// Cub hammering the LAN.
static const int POLL_FAILURES_BEFORE_RESWEEP = 5;
static const uint32_t SWEEP_BACKOFF_MS = 300000;  // 5 minutes

static const uint32_t SERVER_PREF_HASH = 0x50525331;  // "PRS1"

struct ServerStore {
  char host[64];
  uint16_t port;
};
#endif

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
  // An explicit YAML server is pinned: we use it, never sweep, and never forget
  // it. An empty server means discover (remembered server, then mDNS, then the
  // LAN sweep).
  this->server_is_explicit_ = !this->server_.empty();
#ifdef PR_USE_SWEEP
  this->server_pref_ = global_preferences->make_preference<ServerStore>(SERVER_PREF_HASH);
  if (!this->server_is_explicit_)
    this->load_server_();  // try a previously discovered server first on boot
#endif
  if (this->server_.empty())
    this->pairing_state_ = PAIRING_NO_SERVER;
#ifdef PR_USE_BLE
  this->tag_pref_ = global_preferences->make_preference<TagStore>(TAG_PREF_HASH);
  if (!this->ble_tag_pinned_)
    this->load_tag_();
  if (this->transport_ == CUB_TRANSPORT_BLE) {
    // Receive-only: no server, no pairing, ever. The screen shows the
    // listening line until the first broadcast lands.
    this->pairing_state_ = PAIRING_IDLE;
  }
#endif
  // A pinned or remembered server means we already know where firmware comes
  // from, so the first check after boot can go to the right place.
  this->push_ota_url_();
}

void PantryRaiderHub::dump_config() {
  ESP_LOGCONFIG(TAG, "Pantry Raider hub:");
  ESP_LOGCONFIG(TAG, "  Cub id: %s", this->cub_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Profile: %s", this->profile_.c_str());
  ESP_LOGCONFIG(TAG, "  Version: %s", this->version_.c_str());
  if (this->server_is_explicit_) {
    ESP_LOGCONFIG(TAG, "  Server: %s:%u (pinned)", this->server_.c_str(), this->port_);
  } else if (this->server_.empty()) {
#ifdef PR_USE_SWEEP
    ESP_LOGCONFIG(TAG, "  Server: (auto: remembered, mDNS, then LAN sweep)");
#else
    ESP_LOGCONFIG(TAG, "  Server: (auto: mDNS discovery)");
#endif
  } else {
    ESP_LOGCONFIG(TAG, "  Server: %s:%u (discovered)", this->server_.c_str(), this->port_);
  }
  ESP_LOGCONFIG(TAG, "  Paired: %s", this->paired() ? "yes" : "no");
  if (this->ota_manifest_url_.empty()) {
    ESP_LOGCONFIG(TAG, "  Firmware manifest: (follows the server)");
  } else {
    ESP_LOGCONFIG(TAG, "  Firmware manifest: %s (pinned)", this->ota_manifest_url_.c_str());
  }
  static const char *const TRANSPORT_NAMES[] = {"lan", "ble", "auto"};
  ESP_LOGCONFIG(TAG, "  Transport: %s", TRANSPORT_NAMES[this->transport_]);
#ifdef PR_USE_BLE
  if (this->ble_tag_pinned_) {
    ESP_LOGCONFIG(TAG, "  Install tag: %02x%02x%02x%02x (pinned)", this->ble_tag_[0], this->ble_tag_[1],
                  this->ble_tag_[2], this->ble_tag_[3]);
  } else if (this->ble_tag_set_) {
    ESP_LOGCONFIG(TAG, "  Install tag: %02x%02x%02x%02x (remembered)", this->ble_tag_[0], this->ble_tag_[1],
                  this->ble_tag_[2], this->ble_tag_[3]);
  } else {
    ESP_LOGCONFIG(TAG, "  Install tag: (first sender heard wins)");
  }
#endif
}

void PantryRaiderHub::update() {
#ifdef PR_USE_BLE
  if (this->transport_ == CUB_TRANSPORT_BLE) {
    // Receive-only: nothing to poll, no server to find. Publishing here keeps
    // the HA sensors ticking (the countdown derives from deadline_epoch).
    this->publish_();
    return;
  }
#endif
  if (this->server_.empty()) {
#ifdef PR_USE_SWEEP
    if (this->sweep_phase_ != SWEEP_NONE) {
      // A sweep is running in loop(); let it finish before doing anything else.
      this->publish_();
      return;
    }
#endif
#ifdef PR_USE_DISCOVERY
    this->discover_();  // mDNS: instant on a real LAN, silent behind a bridge
#endif
#ifdef PR_USE_SWEEP
    if (this->server_.empty()) {
      // mDNS found nothing (the bridge-networked Docker case): sweep the LAN.
      this->start_sweep_();
      this->publish_();
      return;
    }
#endif
    if (this->server_.empty()) {
      this->pairing_state_ = PAIRING_NO_SERVER;
      this->publish_();
      return;
    }
  }
  if (this->api_key_.empty()) {
    // Kick off (or keep retrying) pairing once we have a server. REQUESTING is
    // included so a freshly discovered server, from mDNS or the LAN sweep,
    // actually starts asking to join instead of stalling. start_pairing_ is a
    // no-op while a code is already on screen (WAITING).
    if (this->pairing_state_ == PAIRING_IDLE || this->pairing_state_ == PAIRING_NO_SERVER ||
        this->pairing_state_ == PAIRING_REQUESTING)
      this->start_pairing_();
    this->publish_();
    return;
  }
  this->poll_summary_();
}

void PantryRaiderHub::loop() {
#ifdef PR_USE_BLE_RELAY
  // Send a batch once it is full, or once the oldest packet in it has waited
  // long enough. Whichever comes first, so a busy kitchen posts in bursts of
  // ten and a quiet one still reports within a couple of seconds.
  if (this->relay_queue_n_ > 0 &&
      (this->relay_queue_n_ >= this->relay_batch_max_ ||
       (millis() - this->relay_first_ms_) >= this->relay_batch_ms_))
    this->relay_flush_();
#endif
#ifdef PR_USE_SWEEP
  // Drive the LAN sweep one batch per loop() so the display stays live and the
  // watchdog never trips. Cheap no-op when no sweep is in flight.
  if (this->sweep_phase_ != SWEEP_NONE)
    this->step_sweep_();
#endif
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
    this->poll_failures_ = 0;
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
#ifdef PR_USE_SWEEP
    // A discovered server that keeps failing may have changed IP: forget it and
    // rediscover. A pinned YAML server is never forgotten.
    this->poll_failures_++;
    if (!this->server_is_explicit_ && this->poll_failures_ >= POLL_FAILURES_BEFORE_RESWEEP) {
      ESP_LOGW(TAG, "Server unreachable %d times; forgetting %s and rediscovering", this->poll_failures_,
               this->server_.c_str());
      this->poll_failures_ = 0;
      this->server_.clear();
      this->save_server_("", 0);  // drop the stale cache so mDNS/sweep can win
      this->pairing_state_ = PAIRING_NO_SERVER;
    }
#endif
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
      // Read the deadline as a double, then take the whole seconds. Asking the
      // JSON reader for an integer looks right and is a trap: `| (int64_t) 0`
      // hands back the default for anything that is not stored as an integer,
      // and a server that sends 1784164563.358 stores a float, so every
      // deadline came back 0 and the screen sat at 0:00 (FoodAssistant-8qtx).
      // A double reads both shapes, and 2^53 seconds is not a date anyone here
      // will see.
      timer.deadline_epoch = (int64_t) (t["deadline_epoch"] | 0.0);
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
      st.settings.auto_update = settings["auto_update"] | true;
    }
    this->state_ = std::move(st);
    this->state_millis_ = millis();
#ifdef PR_USE_BLE
    this->state_from_ble_ = false;  // a live LAN summary always wins
#endif
#ifdef PR_USE_BLE_RELAY
    // The server decides what this Cub listens for, every poll. A server with
    // the relay off (or one too old to know about it) sends no block, and the
    // radio work stops until it does.
    this->relay_read_allowlist_(root);
#endif
    return true;
  });
  if (!ok)
    ESP_LOGW(TAG, "Could not parse /cub/summary reply");
  return ok;
}

// --- Automatic firmware updates ----------------------------------------------

// How long a version that failed to install waits before it is offered to the
// flasher again. Long enough that a bad image costs a Cub nothing, short
// enough that a fixed server heals the kitchen the same day.
static const uint32_t OTA_RETRY_MS = 6 * 60 * 60 * 1000UL;

// "0.18.47" > "0.18.9": compare the dotted numbers, not the text. Anything
// unparseable (an empty string, a hand-built firmware with a name for a
// version) reads as "not newer", so a Cub sits still rather than guessing.
static bool version_newer(const std::string &a, const std::string &b) {
  if (a.empty() || b.empty())
    return false;
  const char *pa = a.c_str();
  const char *pb = b.c_str();
  for (int i = 0; i < 4; i++) {
    char *end_a = nullptr;
    char *end_b = nullptr;
    long va = strtol(pa, &end_a, 10);
    long vb = strtol(pb, &end_b, 10);
    if (end_a == pa && end_b == pb)
      return false;  // neither side has a number left: equal as far as we read
    if (va != vb)
      return va > vb;
    pa = (*end_a == '.') ? end_a + 1 : end_a;
    pb = (*end_b == '.') ? end_b + 1 : end_b;
  }
  return false;
}

std::string PantryRaiderHub::ota_url_() const {
  if (!this->ota_manifest_url_.empty())
    return this->ota_manifest_url_;  // pinned in YAML: never retargeted
  if (this->server_.empty())
    return "";  // no server found yet; nothing to check against
  return this->base_url_() + "/cub/firmware/manifest.json?profile=" + this->profile_;
}

void PantryRaiderHub::push_ota_url_() {
  if (!this->ota_url_cb_)
    return;  // no update entity in this build: nothing to retarget
  std::string url = this->ota_url_();
  if (url.empty() || url == this->ota_url_sent_)
    return;
  this->ota_url_sent_ = url;
  ESP_LOGI(TAG, "Firmware updates will check %s", url.c_str());
  this->ota_url_cb_(url);
}

bool PantryRaiderHub::ota_quiet() const {
  // A code on screen is someone standing at the device, mid-setup.
  if (this->pairing_state_ != PAIRING_IDLE || !this->pairing_code_.empty())
    return false;
  // An alarm is on screen: groceries are at stake and the screen is the point.
  if (this->state_.attention || this->state_.view == "alert")
    return false;
  // A timer is ringing and waiting to be dismissed.
  for (const auto &t : this->state_.timers) {
    if (t.expired || this->timer_remaining(t) <= 0)
      return false;
  }
  return true;
}

bool PantryRaiderHub::ota_install_ready(const std::string &latest_version) const {
  // Only ever act on what a live, paired server said. An unpaired Cub, or one
  // whose server is down, has no business flashing anything.
  if (!this->online_ || !this->state_.valid || !this->paired())
    return false;
  if (!this->state_.settings.auto_update)
    return false;
  // Match the server, but never walk backwards: a server on an older version
  // than the Cub leaves the Cub alone.
  if (!version_newer(latest_version, this->version_))
    return false;
  // This exact version already had its turn and we are still here, so it did
  // not take. Leave it alone for a while rather than grinding on it.
  if (!this->ota_tried_version_.empty() && latest_version == this->ota_tried_version_ &&
      (millis() - this->ota_tried_ms_) < OTA_RETRY_MS)
    return false;
  return this->ota_quiet();
}

void PantryRaiderHub::ota_mark_attempt(const std::string &version) {
  this->ota_tried_version_ = version;
  this->ota_tried_ms_ = millis();
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
  if (status == 403 && !this->server_is_explicit_) {
    // A 403 means this install refuses to pair at all (a satellite, or a
    // server with device pairing turned off). Retrying the same address can
    // never succeed, so forget the discovered server and go find another
    // one instead of looping forever.
    ESP_LOGW(TAG, "Server %s refuses pairing (403); forgetting it and rediscovering", this->server_.c_str());
    this->server_.clear();
#ifdef PR_USE_SWEEP
    this->save_server_("", 0);  // drop the cached address so mDNS/sweep can win
#endif
    this->pairing_state_ = PAIRING_NO_SERVER;
    this->publish_();
    return;
  }
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
    // Never adopt a satellite: a pi_remote owns no keys to hand out, so
    // pairing against it can only ever 403 (seen live on a LAN where the
    // only mDNS advertiser was a Bandit). The LAN sweep after this finds a
    // bridge-networked main server; a satellite is only ever used when the
    // owner pins its address explicitly.
    if (mode == "pi_remote")
      continue;
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
    this->poll_failures_ = 0;
#ifdef PR_USE_SWEEP
    // Remember it (host:port stored whole; port 0 means keep the parsed port),
    // so a later boot tries it before browsing again.
    this->save_server_(best, 0);
#endif
    if (this->pairing_state_ == PAIRING_NO_SERVER)
      this->pairing_state_ = this->api_key_.empty() ? PAIRING_REQUESTING : PAIRING_IDLE;
  }
#endif
#ifdef USE_ARDUINO
  int n = MDNS.queryService("pantry-raider", "tcp");
  int best = -1;
  for (int i = 0; i < n; i++) {
    String mode = MDNS.txt(i, "mode");
    if (mode == "pi_remote")
      continue;  // a satellite cannot pair; never adopt one automatically
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

// --- LAN sweep fallback ------------------------------------------------------
//
// mDNS cannot reach a bridge-networked Docker server (Dan's Korolev), so when
// it comes up empty we sweep the local /24 ourselves: non-blocking TCP connects
// in small batches (one batch per loop() iteration), then a short /health GET
// on each open host, keeping the ones that fingerprint as Pantry Raider and are
// not a satellite. This mirrors service/app/services/lan_scan.py.

#ifdef PR_USE_SWEEP

void PantryRaiderHub::load_server_() {
  ServerStore stored{};
  if (this->server_pref_.load(&stored)) {
    stored.host[sizeof(stored.host) - 1] = '\0';
    if (stored.host[0] != '\0') {
      this->server_ = stored.host;
      if (stored.port != 0)
        this->port_ = stored.port;
      ESP_LOGI(TAG, "Trying remembered server %s:%u", this->server_.c_str(), this->port_);
    }
  }
}

void PantryRaiderHub::save_server_(const std::string &host, uint16_t port) {
  ServerStore stored{};
  strncpy(stored.host, host.c_str(), sizeof(stored.host) - 1);
  stored.port = port;
  this->server_pref_.save(&stored);
  global_preferences->sync();
}

void PantryRaiderHub::adopt_server_(const std::string &host, uint16_t port) {
  this->server_ = host;
  this->port_ = port;
  this->save_server_(host, port);  // remember it so later boots skip the sweep
  this->poll_failures_ = 0;
  ESP_LOGI(TAG, "Using discovered server %s:%u", host.c_str(), port);
  if (this->pairing_state_ == PAIRING_NO_SERVER || this->pairing_state_ == PAIRING_SEARCHING)
    this->pairing_state_ = this->api_key_.empty() ? PAIRING_REQUESTING : PAIRING_IDLE;
  // Act on the new server now rather than waiting for the next poll tick.
  this->set_timeout("pr_first_poll", 500, [this]() { this->update(); });
}

void PantryRaiderHub::start_sweep_() {
  if (this->sweep_phase_ != SWEEP_NONE)
    return;  // already sweeping
  uint32_t now = millis();
  if (this->sweep_ever_ran_ && (now - this->last_sweep_ms_) < SWEEP_BACKOFF_MS) {
    ESP_LOGD(TAG, "LAN sweep on backoff; waiting before another sweep");
    return;
  }

  esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  esp_netif_ip_info_t info{};
  if (netif == nullptr || esp_netif_get_ip_info(netif, &info) != ESP_OK || info.ip.addr == 0) {
    ESP_LOGW(TAG, "LAN sweep: station has no IP yet; will retry");
    return;
  }
  uint32_t ip_host = ntohl(info.ip.addr);
  uint32_t mask_host = ntohl(info.netmask.addr);
  if (mask_host != 0 && mask_host < 0xFFFFFF00u) {
    // Wider than a /24: we still sweep only the local /24 around ourselves so a
    // sweep never balloons past 254 hosts.
    ESP_LOGI(TAG, "LAN is wider than /24; sweeping only the local /24 around us");
  }
  this->sweep_net_ = ip_host & 0xFFFFFF00u;
  this->sweep_self_ = ip_host & 0xFFu;
  this->sweep_idx_ = 1;
  this->sweep_port_ = SWEEP_PORTS[0];
  this->sweep_opened_.assign(256, false);
  this->sweep_hits_.clear();
  this->sweep_cands_.clear();
  this->sweep_phase_ = SWEEP_CONNECT;
  this->last_sweep_ms_ = now;
  this->sweep_ever_ran_ = true;
  this->pairing_state_ = PAIRING_SEARCHING;
  ESP_LOGI(TAG, "Scanning LAN %u.%u.%u.0/24 for a Pantry Raider server", (unsigned) (this->sweep_net_ >> 24),
           (unsigned) ((this->sweep_net_ >> 16) & 0xFF), (unsigned) ((this->sweep_net_ >> 8) & 0xFF));
}

void PantryRaiderHub::step_sweep_() {
  if (this->sweep_phase_ == SWEEP_HEALTH) {
    // Fingerprint one open host per loop() so a slow /health cannot stall us.
    if (this->sweep_hits_.empty()) {
      this->finish_sweep_();
      return;
    }
    SweepHit hit = this->sweep_hits_.back();
    this->sweep_hits_.pop_back();
    std::string mode;
    if (this->health_probe_(htonl(hit.ip), hit.port, mode)) {
      if (mode == "pi_remote") {
        // A satellite cannot pair (its pairing endpoint 403s), so adopting
        // one only strands the Cub in a retry loop. Log it and move on; a
        // satellite is only used when the owner pins its address in YAML.
        ESP_LOGI(TAG, "Sweep skipping satellite at %u.%u.%u.%u:%u", (unsigned) ((hit.ip >> 24) & 0xFF),
                 (unsigned) ((hit.ip >> 16) & 0xFF), (unsigned) ((hit.ip >> 8) & 0xFF), (unsigned) (hit.ip & 0xFF),
                 hit.port);
        return;
      }
      uint8_t rank = 1;  // empty/unknown mode: a plain Docker server reports this
      if (mode == "server" || mode == "pi_hosted")
        rank = 0;
      SweepCandidate c;
      c.ip = hit.ip;  // already host byte order
      c.port = hit.port;
      c.rank = rank;
      this->sweep_cands_.push_back(c);
      ESP_LOGI(TAG, "Sweep found Pantry Raider at %u.%u.%u.%u:%u (mode '%s')", (unsigned) ((c.ip >> 24) & 0xFF),
               (unsigned) ((c.ip >> 16) & 0xFF), (unsigned) ((c.ip >> 8) & 0xFF), (unsigned) (c.ip & 0xFF), c.port,
               mode.c_str());
    }
    return;
  }

  // SWEEP_CONNECT: launch a batch of non-blocking connects and see which open.
  int launched = 0;
  int socks[SWEEP_BATCH];
  uint32_t targets[SWEEP_BATCH];  // host-order full address per socket
  int maxfd = -1;
  fd_set wset;
  FD_ZERO(&wset);

  while (launched < SWEEP_BATCH && this->sweep_idx_ <= 254) {
    uint8_t host = (uint8_t) this->sweep_idx_++;
    if (host == this->sweep_self_)
      continue;  // skip ourselves (we answer /health too)
    if (this->sweep_opened_[host])
      continue;  // already opened on an earlier port pass
    uint32_t addr_host = this->sweep_net_ | host;
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
      break;  // out of sockets; try again next loop
    int flags = ::fcntl(fd, F_GETFL, 0);
    ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    struct sockaddr_in sa{};
    sa.sin_family = AF_INET;
    sa.sin_port = htons(this->sweep_port_);
    sa.sin_addr.s_addr = htonl(addr_host);
    int rc = ::connect(fd, (struct sockaddr *) &sa, sizeof(sa));
    if (rc == 0) {
      // Immediate connect (rare on a LAN): record and move on.
      this->sweep_opened_[host] = true;
      this->sweep_hits_.push_back({addr_host, this->sweep_port_});
      ::close(fd);
      continue;
    }
    if (errno != EINPROGRESS) {
      ::close(fd);
      continue;  // refused/unreachable outright
    }
    socks[launched] = fd;
    targets[launched] = addr_host;
    FD_SET(fd, &wset);
    if (fd > maxfd)
      maxfd = fd;
    launched++;
  }

  if (launched > 0) {
    // select() wakes on the FIRST completion (a live neighbor refusing with
    // an instant RST counts), so a single early riser must not cost the rest
    // of the batch its connect window: resolve the ready ones, then keep
    // selecting on whatever is still in flight until the window is spent.
    // Missing this dropped a real server from the sweep on the first live
    // test (a fast RST elsewhere in the batch aborted the whole window).
    int open_count = launched;
    uint32_t start = millis();
    while (open_count > 0) {
      uint32_t elapsed = millis() - start;
      if (elapsed >= SWEEP_CONNECT_MS)
        break;
      fd_set pending;
      FD_ZERO(&pending);
      maxfd = -1;
      for (int i = 0; i < launched; i++) {
        if (socks[i] < 0)
          continue;
        FD_SET(socks[i], &pending);
        if (socks[i] > maxfd)
          maxfd = socks[i];
      }
      uint32_t remain = SWEEP_CONNECT_MS - elapsed;
      struct timeval tv{};
      tv.tv_sec = remain / 1000;
      tv.tv_usec = (remain % 1000) * 1000;
      if (::select(maxfd + 1, nullptr, &pending, nullptr, &tv) <= 0)
        break;  // window elapsed with nothing new
      for (int i = 0; i < launched; i++) {
        int fd = socks[i];
        if (fd < 0 || !FD_ISSET(fd, &pending))
          continue;
        int err = 0;
        socklen_t len = sizeof(err);
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) == 0 && err == 0) {
          uint8_t host = (uint8_t) (targets[i] & 0xFF);
          this->sweep_opened_[host] = true;
          this->sweep_hits_.push_back({targets[i], this->sweep_port_});
        }
        ::close(fd);
        socks[i] = -1;
        open_count--;
      }
    }
    for (int i = 0; i < launched; i++) {
      if (socks[i] >= 0)
        ::close(socks[i]);
    }
  }

  if (this->sweep_idx_ > 254) {
    // Finished this port pass. Move to the next port, or to fingerprinting.
    if (this->sweep_port_ == SWEEP_PORTS[0]) {
      this->sweep_port_ = SWEEP_PORTS[1];
      this->sweep_idx_ = 1;
    } else {
      this->sweep_phase_ = SWEEP_HEALTH;
    }
  }
}

void PantryRaiderHub::finish_sweep_() {
  this->sweep_phase_ = SWEEP_NONE;
  this->sweep_opened_.clear();
  this->sweep_opened_.shrink_to_fit();
  if (this->sweep_cands_.empty()) {
    ESP_LOGW(TAG, "LAN sweep found no Pantry Raider server; will retry later");
    this->pairing_state_ = PAIRING_NO_SERVER;
    return;
  }
  // Best candidate: lowest rank (server/pi_hosted, then unknown, then a
  // satellite), ties broken by lowest IP.
  const SweepCandidate *best = &this->sweep_cands_[0];
  for (const auto &c : this->sweep_cands_) {
    if (c.rank < best->rank || (c.rank == best->rank && c.ip < best->ip))
      best = &c;
  }
  char buf[24];
  snprintf(buf, sizeof(buf), "%u.%u.%u.%u", (unsigned) ((best->ip >> 24) & 0xFF), (unsigned) ((best->ip >> 16) & 0xFF),
           (unsigned) ((best->ip >> 8) & 0xFF), (unsigned) (best->ip & 0xFF));
  this->adopt_server_(buf, best->port);
  this->sweep_cands_.clear();
}

bool PantryRaiderHub::health_probe_(uint32_t ip_net, uint16_t port, std::string &mode_out) {
  // A self-contained, short-timeout GET /health over a raw socket, so a sweep
  // never blocks on the shared http_request 10s timeout. Returns true only when
  // the reply fingerprints as Pantry Raider (app == "foodassistant").
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    return false;
  int flags = ::fcntl(fd, F_GETFL, 0);
  ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
  struct sockaddr_in sa{};
  sa.sin_family = AF_INET;
  sa.sin_port = htons(port);
  sa.sin_addr.s_addr = ip_net;

  bool ok = false;
  do {
    int rc = ::connect(fd, (struct sockaddr *) &sa, sizeof(sa));
    if (rc != 0 && errno != EINPROGRESS)
      break;
    if (rc != 0) {
      fd_set wset;
      FD_ZERO(&wset);
      FD_SET(fd, &wset);
      struct timeval tv{};
      tv.tv_sec = SWEEP_HEALTH_MS / 1000;
      tv.tv_usec = (SWEEP_HEALTH_MS % 1000) * 1000;
      if (::select(fd + 1, nullptr, &wset, nullptr, &tv) <= 0)
        break;
      int err = 0;
      socklen_t len = sizeof(err);
      if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) != 0 || err != 0)
        break;
    }
    char req[128];
    int n = snprintf(req, sizeof(req),
                     "GET /health HTTP/1.0\r\nHost: %u.%u.%u.%u\r\nConnection: close\r\n\r\n",
                     (unsigned) (ntohl(ip_net) >> 24 & 0xFF), (unsigned) (ntohl(ip_net) >> 16 & 0xFF),
                     (unsigned) (ntohl(ip_net) >> 8 & 0xFF), (unsigned) (ntohl(ip_net) & 0xFF));
    if (::send(fd, req, n, 0) != n)
      break;

    std::string resp;
    uint32_t start = millis();
    while (resp.size() < 2048 && (millis() - start) < SWEEP_HEALTH_MS) {
      fd_set rset;
      FD_ZERO(&rset);
      FD_SET(fd, &rset);
      struct timeval tv{};
      tv.tv_usec = 100000;  // 100ms slices
      if (::select(fd + 1, &rset, nullptr, nullptr, &tv) <= 0)
        continue;
      char chunk[512];
      int got = ::recv(fd, chunk, sizeof(chunk), 0);
      if (got <= 0)
        break;  // peer closed or error: we have the whole reply
      resp.append(chunk, got);
    }
    size_t body_at = resp.find("\r\n\r\n");
    if (body_at == std::string::npos)
      break;
    std::string body = resp.substr(body_at + 4);
    std::string app;
    json::parse_json(body, [&app, &mode_out](JsonObject root) -> bool {
      app = std::string(root["app"] | "");
      mode_out = std::string(root["mode"] | "");
      return true;
    });
    ok = (app == "foodassistant");
  } while (false);

  ::close(fd);
  return ok;
}

#endif  // PR_USE_SWEEP

// --- BLE broadcast receive -----------------------------------------------------
//
// The Pi appliance (gadgets agent, advertiser.py) broadcasts a 23-byte status
// packet: counts, a view hint, the soonest timer, one probe. The Cub passively
// scans for it via esp32_ble_tracker, so a Cub with no Wi-Fi (transport: ble)
// or a Cub whose server just vanished (transport: auto) still shows the
// kitchen. Receive-only and unauthenticated by design; the packet carries
// counts and temperatures only, never names or tokens.

#ifdef PR_USE_BLE

void PantryRaiderHub::set_install_tag(const std::string &hex) {
  if (hex.size() != 8)
    return;  // codegen validated this already; belt and suspenders
  for (int i = 0; i < 4; i++) {
    char byte_hex[3] = {hex[i * 2], hex[i * 2 + 1], '\0'};
    this->ble_tag_[i] = (uint8_t) strtol(byte_hex, nullptr, 16);
  }
  this->ble_tag_set_ = true;
  this->ble_tag_pinned_ = true;
}

void PantryRaiderHub::load_tag_() {
  TagStore stored{};
  if (this->tag_pref_.load(&stored) && stored.set) {
    memcpy(this->ble_tag_, stored.tag, 4);
    this->ble_tag_set_ = true;
  }
}

void PantryRaiderHub::save_tag_() {
  TagStore stored{};
  memcpy(stored.tag, this->ble_tag_, 4);
  stored.set = 1;
  this->tag_pref_.save(&stored);
  global_preferences->sync();
}

bool PantryRaiderHub::ble_fresh() const {
  return this->ble_last_ms_ != 0 && (millis() - this->ble_last_ms_) < BLE_STALE_MS;
}

bool PantryRaiderHub::parse_device(const esp32_ble_tracker::ESPBTDevice &device) {
#ifdef PR_USE_BLE_RELAY
  // The relay and the status-broadcast listener share this callback and do
  // not overlap: a kitchen sensor is never the Cub broadcast, so capturing
  // here costs a matching pass and changes nothing below.
  if (this->relay_active() && this->relay_wanted_(device))
    this->relay_capture_(device);
  // A LAN Cub with the relay on has its radio up purely to relay: it gets its
  // own state from the poll and must never render somebody's broadcast.
  if (this->transport_ == CUB_TRANSPORT_LAN)
    return false;
#endif
  for (const auto &md : device.get_manufacturer_datas()) {
    if (md.uuid != esp32_ble_tracker::ESPBTUUID::from_uint16(CUB_BLE_COMPANY_ID))
      continue;
    CubBlePacket pkt;
    // Wrong length or version fails here, which is what makes squatting on
    // the 0xFFFF prototyping company id safe: stray packets never parse.
    if (!parse_cub_msd(md.data.data(), md.data.size(), pkt))
      continue;
    if (this->ble_tag_set_) {
      if (memcmp(pkt.install_tag, this->ble_tag_, 4) != 0)
        continue;  // another install's broadcast (two-server household)
    } else {
      // First sender heard wins and is remembered in flash, so a second
      // install appearing later can never flip this Cub. Re-flashing with
      // "Erase device" (or pinning install_tag in YAML) resets the choice.
      memcpy(this->ble_tag_, pkt.install_tag, 4);
      this->ble_tag_set_ = true;
      this->save_tag_();
      ESP_LOGI(TAG, "Locked onto broadcast install tag %02x%02x%02x%02x", this->ble_tag_[0], this->ble_tag_[1],
               this->ble_tag_[2], this->ble_tag_[3]);
    }
    this->ble_last_ms_ = millis();
    // auto: a healthy LAN feed wins; the packet is heard but not shown.
    if (this->transport_ == CUB_TRANSPORT_AUTO && this->paired() && this->online_)
      return true;
    // The sender bumps seq only when content changed; an unchanged packet
    // needs no re-apply (the countdown ticks locally from deadline_epoch).
    if (this->state_from_ble_ && (int) pkt.seq == this->ble_applied_seq_)
      return true;
    this->apply_ble_packet_(pkt);
    return true;
  }
  return false;
}

void PantryRaiderHub::apply_ble_packet_(const CubBlePacket &pkt) {
  // The packet has no wall-clock epoch, so countdowns run on a local base:
  // real time when SNTP is synced, else the previous base advanced by uptime,
  // else an arbitrary anchor (only differences ever reach the screen).
  int64_t base = this->now_epoch();
  if (base == 0)
    base = 1600000000;

  CubState st;
  st.valid = true;
  st.settings = this->state_.settings;  // keep units etc. from any LAN past
  switch (pkt.view) {
    case CUB_BLE_VIEW_EXPIRING:
      st.view = "expiring";
      break;
    case CUB_BLE_VIEW_TIMERS:
      st.view = "timers";
      break;
    case CUB_BLE_VIEW_PROBE:
      st.view = "probe";
      break;
    default:
      st.view = "clock";  // idle (also what rotation and alert pack)
      break;
  }
  st.generated = base;
  st.expiring_ok = true;
  st.expired = pkt.expired;
  st.today = 0;  // the packet merges "today" into "soon"
  st.soon = pkt.soon;
  st.pending = pkt.pending;
  st.attention = pkt.attention;
  if (pkt.timer_count > 0) {
    // One synthesized timer stands in for them all: the soonest deadline,
    // ringing when any timer rang. Display lambdas render it unchanged.
    CubTimer t;
    t.id = "";  // broadcast timers carry no id; extend/dismiss need LAN
    t.label = pkt.timer_count == 1 ? "Timer" : "Timers (" + to_string((int) pkt.timer_count) + ")";
    t.deadline_epoch = base + (pkt.has_soonest ? (int64_t) pkt.soonest_s : 0);
    t.expired = pkt.timer_ringing;
    st.timers.push_back(t);
  }
  if (pkt.has_temp) {
    CubProbe p;
    p.id = "ble";
    p.name = pkt.probe_at_target ? "Probe at target" : "Probe";
    p.probe = 1;
    p.temp_c = pkt.temp_tenths / 10.0f;
    // delta is target minus current, so the target reconstructs exactly
    // (within the packet's whole-degree rounding).
    p.target_c = pkt.has_delta ? p.temp_c + (float) pkt.delta_c : NAN;
    st.probes.push_back(p);
  }
  this->state_ = std::move(st);
  this->state_millis_ = millis();
  this->state_from_ble_ = true;
  this->ble_applied_seq_ = pkt.seq;
  this->publish_();
}

#endif  // PR_USE_BLE

#ifdef PR_USE_BLE_RELAY

// --- BLE advertisement relay (FoodAssistant-nn3u) -----------------------------
//
// The Cub is only a radio here. It never decodes anything: it matches an
// advertisement against the allowlist the server sent, and forwards the raw
// bytes. The server owns the decoders, so support for a new sensor reaches
// the whole fleet on the next poll with no reflash.

void PantryRaiderHub::relay_read_allowlist_(JsonObject root) {
  JsonObject block = root["ble_relay"];
  if (block.isNull() || !(block["enabled"] | false)) {
    // No block, or the server turned the relay off: stop scanning for sensors
    // and drop anything still queued (it has nowhere useful to go).
    if (this->relay_allowed_) {
      ESP_LOGI(TAG, "BLE relay: the server turned it off");
      this->relay_allowed_ = false;
      this->relay_queue_n_ = 0;
    }
    return;
  }
  this->relay_company_n_ = 0;
  for (JsonVariant v : block["company_ids"].as<JsonArray>()) {
    if (this->relay_company_n_ >= PR_RELAY_ALLOW_MAX)
      break;
    this->relay_company_[this->relay_company_n_++] = (uint16_t) (v.as<uint32_t>() & 0xFFFF);
  }
  this->relay_uuid_n_ = 0;
  for (JsonVariant v : block["service_uuids"].as<JsonArray>()) {
    if (this->relay_uuid_n_ >= PR_RELAY_ALLOW_MAX)
      break;
    this->relay_uuid_[this->relay_uuid_n_++] = (uint16_t) (v.as<uint32_t>() & 0xFFFF);
  }
  this->relay_names_n_ = 0;
  for (JsonVariant v : block["names"].as<JsonArray>()) {
    if (this->relay_names_n_ >= PR_RELAY_NAMES_MAX)
      break;
    const char *name = v.as<const char *>();
    if (name == nullptr || name[0] == '\0')
      continue;
    // Truncating a prefix would widen the match, so a name that does not fit
    // is skipped rather than cut short.
    if (strlen(name) >= PR_RELAY_NAME_LEN)
      continue;
    strncpy(this->relay_names_[this->relay_names_n_], name, PR_RELAY_NAME_LEN - 1);
    this->relay_names_[this->relay_names_n_][PR_RELAY_NAME_LEN - 1] = '\0';
    this->relay_names_n_++;
  }
  uint32_t batch = block["max_packets"] | (uint32_t) PR_RELAY_QUEUE_MAX;
  this->relay_batch_max_ = (uint8_t) (batch < 1 ? 1 : (batch > PR_RELAY_QUEUE_MAX ? PR_RELAY_QUEUE_MAX : batch));
  uint32_t interval = block["interval_ms"] | (uint32_t) 2000;
  this->relay_batch_ms_ = interval < 250 ? 250 : (interval > 30000 ? 30000 : interval);
  if (!this->relay_allowed_) {
    ESP_LOGI(TAG, "BLE relay on: %u company ids, %u service UUIDs, %u names", this->relay_company_n_,
             this->relay_uuid_n_, this->relay_names_n_);
  }
  this->relay_allowed_ = true;
}

bool PantryRaiderHub::relay_wanted_(const esp32_ble_tracker::ESPBTDevice &device) const {
  for (const auto &md : device.get_manufacturer_datas()) {
    const auto uuid = md.uuid.get_uuid();
    if (uuid.len != ESP_UUID_LEN_16)
      continue;
    for (uint8_t i = 0; i < this->relay_company_n_; i++) {
      if (uuid.uuid.uuid16 == this->relay_company_[i])
        return true;
    }
  }
  for (const auto &sd : device.get_service_datas()) {
    const auto uuid = sd.uuid.get_uuid();
    if (uuid.len != ESP_UUID_LEN_16)
      continue;
    for (uint8_t i = 0; i < this->relay_uuid_n_; i++) {
      if (uuid.uuid.uuid16 == this->relay_uuid_[i])
        return true;
    }
  }
  // The name is the only stable filter for the sensors that roll their
  // temperature bytes through the company id (TempSpike, Inkbird IBS-TH) and
  // for the Govee grills, which are matched by payload shape under any id.
  const std::string &name = device.get_name();
  if (name.empty() || this->relay_names_n_ == 0)
    return false;
  char low[PR_RELAY_NAME_LEN]{};
  size_t n = name.size() < PR_RELAY_NAME_LEN - 1 ? name.size() : PR_RELAY_NAME_LEN - 1;
  for (size_t i = 0; i < n; i++)
    low[i] = (char) tolower((unsigned char) name[i]);
  for (uint8_t i = 0; i < this->relay_names_n_; i++) {
    size_t plen = strlen(this->relay_names_[i]);
    if (plen > 0 && plen <= n && strncmp(low, this->relay_names_[i], plen) == 0)
      return true;
  }
  return false;
}

void PantryRaiderHub::relay_capture_(const esp32_ble_tracker::ESPBTDevice &device) {
  const auto &scan = device.get_scan_result();
  uint16_t len = (uint16_t) scan.adv_data_len + (uint16_t) scan.scan_rsp_len;
  if (len == 0)
    return;
  if (len > PR_RELAY_ADV_MAX)
    len = PR_RELAY_ADV_MAX;  // cannot happen on a real radio; clamp anyway

  // FNV-1a over the payload: one press or one reading arrives as a burst of
  // identical advertisements, and the same packet can be delivered twice.
  uint32_t hash = 2166136261u;
  for (uint16_t i = 0; i < len; i++) {
    hash ^= scan.ble_adv[i];
    hash *= 16777619u;
  }
  const uint32_t now = millis();
  for (uint8_t i = 0; i < PR_RELAY_SEEN_MAX; i++) {
    if (this->relay_seen_[i].hash == hash && memcmp(this->relay_seen_[i].mac, scan.bda, 6) == 0 &&
        (now - this->relay_seen_[i].ms) < PR_RELAY_DEDUPE_MS)
      return;  // the same thing, from the same device, moments ago
  }
  memcpy(this->relay_seen_[this->relay_seen_i_].mac, scan.bda, 6);
  this->relay_seen_[this->relay_seen_i_].hash = hash;
  this->relay_seen_[this->relay_seen_i_].ms = now;
  this->relay_seen_i_ = (uint8_t) ((this->relay_seen_i_ + 1) % PR_RELAY_SEEN_MAX);

  if (this->relay_queue_n_ >= PR_RELAY_QUEUE_MAX) {
    // Full: the server is slow or gone. Keep the newest reading rather than
    // the oldest, and never grow.
    memmove(&this->relay_queue_[0], &this->relay_queue_[1], sizeof(RelayPacket) * (PR_RELAY_QUEUE_MAX - 1));
    this->relay_queue_n_ = PR_RELAY_QUEUE_MAX - 1;
  }
  RelayPacket &pkt = this->relay_queue_[this->relay_queue_n_];
  memcpy(pkt.mac, scan.bda, 6);
  pkt.rssi = scan.rssi;
  pkt.len = (uint8_t) len;
  memcpy(pkt.adv, scan.ble_adv, len);
  if (this->relay_queue_n_ == 0)
    this->relay_first_ms_ = now;
  this->relay_queue_n_++;
}

void PantryRaiderHub::relay_flush_() {
  const uint8_t count = this->relay_queue_n_;
  this->relay_queue_n_ = 0;  // this batch is spoken for either way
  if (count == 0)
    return;
  // Nothing to send it to: an unpaired Cub, or one that has not found the
  // server yet. The relay is best-effort, so the batch is simply dropped.
  if (this->api_key_.empty() || this->server_.empty())
    return;

  static const char *const HEX = "0123456789abcdef";
  std::string body;
  body.reserve(64 + (size_t) count * 160);
  body = "{\"packets\":[";
  for (uint8_t i = 0; i < count; i++) {
    const RelayPacket &pkt = this->relay_queue_[i];
    if (i > 0)
      body += ',';
    body += "{\"mac\":\"";
    for (uint8_t b = 0; b < 6; b++) {
      if (b > 0)
        body += ':';
      body += HEX[pkt.mac[b] >> 4];
      body += HEX[pkt.mac[b] & 0x0F];
    }
    body += "\",\"rssi\":";
    body += to_string((int) pkt.rssi);
    body += ",\"adv\":\"";
    for (uint8_t b = 0; b < pkt.len; b++) {
      body += HEX[pkt.adv[b] >> 4];
      body += HEX[pkt.adv[b] & 0x0F];
    }
    body += "\"}";
  }
  body += "]}";

  std::string reply;
  int status = this->fetch_("POST", "/cub/ble-adv", body, reply);
  if (status == 403) {
    // The server turned the relay off since the last poll. Stop until the next
    // summary says otherwise, rather than knocking every two seconds.
    ESP_LOGI(TAG, "BLE relay: the server is no longer accepting; pausing");
    this->relay_allowed_ = false;
  } else if (status != 200) {
    ESP_LOGD(TAG, "BLE relay: %u packets went nowhere (status %d)", count, status);
  }
}

#endif  // PR_USE_BLE_RELAY

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
#ifdef PR_USE_BLE
  // A broadcast-fed state shows as long as it is fresh; in auto this is
  // exactly the "LAN is down, BLE fills in" moment. Once stale it falls
  // through to the usual pairing/offline logic below.
  if (this->state_from_ble_ && this->state_.valid && this->ble_fresh())
    return this->state_.view;
  if (this->transport_ == CUB_TRANSPORT_BLE) {
    // Receive-only Cubs never pair: before the first packet the pairing
    // screen shows the listening line; after a sender goes quiet, offline.
    if (this->state_.valid)
      return this->ble_fresh() ? this->state_.view : "offline";
    return "pairing";
  }
#endif
  if (this->api_key_.empty())
    return "pairing";
  if (!this->state_.valid)
    return this->online_ ? "clock" : "offline";
  return this->state_.view;
}

std::string PantryRaiderHub::pairing_line() const {
#ifdef PR_USE_BLE
  if (this->transport_ == CUB_TRANSPORT_BLE)
    return "Listening for broadcast...";
#endif
  switch (this->pairing_state_) {
    case PAIRING_NO_SERVER:
      return "Looking for Pantry Raider...";
    case PAIRING_SEARCHING:
      return "Scanning the network...";
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
  // Every path through update() ends here, so this is the one place that has
  // to notice the server address changing. It is a string compare on a poll
  // tick; it costs nothing and it means the firmware check always points at
  // the server this Cub actually found.
  this->push_ota_url_();
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
