// YAML actions: pantry_raider.press, pantry_raider.timer_extend,
// pantry_raider.timer_dismiss. Thin templatable wrappers over the hub calls.
#pragma once

#include "esphome/core/automation.h"
#include "pantry_raider.h"

namespace esphome {
namespace pantry_raider {

template<typename... Ts> class PressAction : public Action<Ts...>, public Parented<PantryRaiderHub> {
 public:
  TEMPLATABLE_VALUE(std::string, button)
  TEMPLATABLE_VALUE(bool, long_press)

  // Signature must match Action<Ts...>::play exactly (const Ts &...): with an
  // empty pack both spellings collapse to play(), but LVGL instantiates
  // actions with real args (bool, lv_event_t*) and a by-value play(Ts...)
  // then fails to override.
  void play(const Ts &...x) override {
    this->parent_->press(this->button_.value(x...), this->long_press_.value(x...));
  }
};

template<typename... Ts> class TimerExtendAction : public Action<Ts...>, public Parented<PantryRaiderHub> {
 public:
  TEMPLATABLE_VALUE(std::string, timer_id)
  TEMPLATABLE_VALUE(int, seconds)

  void play(const Ts &...x) override {
    this->parent_->timer_extend(this->timer_id_.value(x...), this->seconds_.value(x...));
  }
};

template<typename... Ts> class TimerDismissAction : public Action<Ts...>, public Parented<PantryRaiderHub> {
 public:
  TEMPLATABLE_VALUE(std::string, timer_id)

  void play(const Ts &...x) override { this->parent_->timer_dismiss(this->timer_id_.value(x...)); }
};

}  // namespace pantry_raider
}  // namespace esphome
