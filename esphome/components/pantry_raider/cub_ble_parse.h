// Pure parser for the Pantry Raider BLE status broadcast.
//
// The Pi side packs this packet in gadgets/foodassistant_gadgets/advertiser.py
// (the authoritative layout comment lives there); this header is the mirror
// the receiver firmware runs on every 0xFFFF manufacturer-data advertisement.
// Both sides are tested against the shared vectors in
// tests/data/cub_ble_vectors.json (see check_vectors.py next to this file).
//
// Deliberately freestanding: no ESPHome includes, so the exact same code
// compiles host-side with g++ for the vector-agreement check.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace esphome {
namespace pantry_raider {

static const uint8_t CUB_BLE_FORMAT_VERSION = 1;
static const uint16_t CUB_BLE_COMPANY_ID = 0xFFFF;
// The manufacturer-specific payload after the company id:
// version, seq, view byte, four counts, soonest u16, temp i16, delta i8, tag[4].
static const size_t CUB_BLE_MSD_LEN = 16;
// Sentinels, matching the packer.
static const uint16_t CUB_BLE_NO_TIMER = 0xFFFF;
static const int16_t CUB_BLE_NO_TEMP = 0x7FFF;
static const int8_t CUB_BLE_NO_DELTA = 0x7F;

// View hint values (the low nibble of the view byte).
enum CubBleView : uint8_t {
  CUB_BLE_VIEW_IDLE = 0,  // idle/clock (also what rotation and alert pack)
  CUB_BLE_VIEW_EXPIRING = 1,
  CUB_BLE_VIEW_TIMERS = 2,
  CUB_BLE_VIEW_PROBE = 3,
};

struct CubBlePacket {
  uint8_t version{0};
  uint8_t seq{0};
  uint8_t view{0};  // low nibble only
  bool timer_ringing{false};
  bool probe_at_target{false};
  bool attention{false};
  uint8_t expired{0};
  uint8_t soon{0};  // includes items expiring today (the packer merges them)
  uint8_t pending{0};
  uint8_t timer_count{0};
  bool has_soonest{false};
  uint16_t soonest_s{0};
  bool has_temp{false};
  int16_t temp_tenths{0};  // tenths of a degree C
  bool has_delta{false};
  int8_t delta_c{0};  // target minus current, whole degrees C
  uint8_t install_tag[4]{0, 0, 0, 0};
};

// Parse the manufacturer-specific payload as the ESP32 BLE stack hands it
// over: the bytes AFTER the two-byte company id (16 bytes, version through
// install tag). Returns false on any length or version mismatch; stray
// 0xFFFF packets from other vendors fail here harmlessly.
inline bool parse_cub_msd(const uint8_t *msd, size_t len, CubBlePacket &out) {
  if (msd == nullptr || len != CUB_BLE_MSD_LEN)
    return false;
  if (msd[0] != CUB_BLE_FORMAT_VERSION)
    return false;
  out.version = msd[0];
  out.seq = msd[1];
  uint8_t view_byte = msd[2];
  out.view = view_byte & 0x0F;
  out.timer_ringing = (view_byte & 0x10) != 0;
  out.probe_at_target = (view_byte & 0x20) != 0;
  out.attention = (view_byte & 0x40) != 0;
  out.expired = msd[3];
  out.soon = msd[4];
  out.pending = msd[5];
  out.timer_count = msd[6];
  uint16_t soonest = (uint16_t) (msd[7] | (msd[8] << 8));
  out.has_soonest = soonest != CUB_BLE_NO_TIMER;
  out.soonest_s = out.has_soonest ? soonest : 0;
  int16_t tenths = (int16_t) (uint16_t) (msd[9] | (msd[10] << 8));
  out.has_temp = tenths != CUB_BLE_NO_TEMP;
  out.temp_tenths = out.has_temp ? tenths : 0;
  int8_t delta = (int8_t) msd[11];
  out.has_delta = delta != CUB_BLE_NO_DELTA;
  out.delta_c = out.has_delta ? delta : 0;
  memcpy(out.install_tag, msd + 12, 4);
  return true;
}

// Parse a full 23-byte advertisement (Flags AD + MSD AD + company id +
// payload), the exact shape in the shared test vectors. On air the stack
// splits the AD structures apart, so the firmware path uses parse_cub_msd
// directly; this wrapper exists for the vector check and for raw captures.
inline bool parse_cub_advertisement(const uint8_t *adv, size_t len, CubBlePacket &out) {
  static const uint8_t HEADER[7] = {0x02, 0x01, 0x06, 0x13, 0xFF, 0xFF, 0xFF};
  if (adv == nullptr || len != sizeof(HEADER) + CUB_BLE_MSD_LEN)
    return false;
  if (memcmp(adv, HEADER, sizeof(HEADER)) != 0)
    return false;
  return parse_cub_msd(adv + sizeof(HEADER), CUB_BLE_MSD_LEN, out);
}

}  // namespace pantry_raider
}  // namespace esphome
