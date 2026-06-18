#!/usr/bin/env python3
"""
Apply ZMK PR #3110 out-of-tree: queue input events over BLE.

Replaces synchronous bt_gatt_notify() in the peripheral input-split path
with a K_MSGQ + service_work_q pattern (matching how position and sensor
events already work).

v2 (post-bb594f2 follow-up): also hardens all three split-peripheral
notification paths against the post-disconnect error storm observed in
production logs at bb594f2:
  - work callback now purges the queue and bails on -ENOTCONN/-EACCES
    (previously drained the entire backlog at ~10ms per BLE event,
     producing a 7-second -ENOTCONN flood after every supervision timeout)
  - new zmk_split_bt_purge_split_queues() helper called from the BT
    disconnected() handler purges position + sensor + input queues
  - is_connected guards at the entry of all three send_*() functions
    stop new events from being queued against a dead link
  - log level of the per-event error downgraded from LOG_ERR to LOG_DBG
    to match upstream sibling paths (position/sensor already use DBG)

Usage: python3 apply_pr3110.py <zmk_module_dir>
   e.g. python3 apply_pr3110.py /tmp/zmk-config/zmk
"""

import sys
import os

SERVICE_C_REL = "app/src/split/bluetooth/service.c"
SERVICE_H_REL = "app/src/split/bluetooth/service.h"
PERIPHERAL_C_REL = "app/src/split/bluetooth/peripheral.c"
KCONFIG_REL = "app/src/split/bluetooth/Kconfig"

# ── service.c: input queue + hardened callback + purge helper ───────

# ── Block 1: forward declaration (BEFORE all send_* functions, OUTSIDE
#    any #if IS_ENABLED(CONFIG_ZMK_INPUT_SPLIT) guard, so it's visible to
#    send_position_state and send_sensor_state too).
SERVICE_EARLY_MARKER = "K_THREAD_STACK_DEFINE(service_q_stack"

SERVICE_EARLY_BLOCK = """/* Forward decl — defined in peripheral.c, exposed via the public header
 * <zmk/split/bluetooth/peripheral.h>. Kept here (unconditionally, before
 * any CONFIG_ZMK_INPUT_SPLIT guard) so send_position_state and
 * send_sensor_state can use it too. */
bool zmk_split_bt_peripheral_is_connected(void);

"""

# ── Block 2: input queue + hardened callback (inside the existing
#    #if IS_ENABLED(CONFIG_ZMK_INPUT_SPLIT) block, before report_input)
SERVICE_INSERT_MARKER = "static int zmk_split_bt_report_input("

SERVICE_INSERT_BLOCK = """struct input_event_notify_item {
    uint16_t attr_index;
    struct zmk_split_input_event_payload payload;
};

K_MSGQ_DEFINE(input_event_msgq, sizeof(struct input_event_notify_item),
              CONFIG_ZMK_INPUT_SPLIT_MSG_QUEUE_SIZE, 4);

static void send_input_event_callback(struct k_work *work) {
    struct input_event_notify_item item;
    while (k_msgq_get(&input_event_msgq, &item, K_NO_WAIT) == 0) {
        int err = bt_gatt_notify(NULL, &split_svc.attrs[item.attr_index], &item.payload,
                                 sizeof(item.payload));
        if (err) {
            if (err == -ENOTCONN || err == -EACCES) {
                /* Link is gone (or not yet encrypted). Drop the backlog
                 * and bail; otherwise service_work_q drains the whole
                 * queue at ~10ms per BLE connection event, flooding the
                 * log for seconds after every disconnect. */
                k_msgq_purge(&input_event_msgq);
                break;
            }
            LOG_DBG("Error notifying input event %d", err);
        }
    }
}

static K_WORK_DEFINE(service_input_notify_work, send_input_event_callback);

"""

# ── Block 3: purge helper (AFTER the #endif that closes
#    #if IS_ENABLED(CONFIG_ZMK_INPUT_SPLIT), so all three queues
#    — position_state_msgq, sensor_state_msgq, input_event_msgq — exist
#    before this function references them. This function itself is
#    UNCONDITIONAL so it links even on boards without CONFIG_ZMK_INPUT_SPLIT
#    like plain sweep_right).
SERVICE_PURGE_MARKER = "#endif /* IS_ENABLED(CONFIG_ZMK_INPUT_SPLIT) */\n\nstatic int service_init(void) {"

SERVICE_PURGE_REPLACEMENT = """#endif /* IS_ENABLED(CONFIG_ZMK_INPUT_SPLIT) */

/* Drop all queued split-peripheral notifications. Called by the BT
 * disconnected() handler to prevent the work_q from draining stale
 * events against a dead link. Also purges the position and sensor
 * queues, which have the same latent drain-on-disconnect bug.
 *
 * MUST be outside any CONFIG_ZMK_INPUT_SPLIT guard: peripheral.c calls
 * it unconditionally from disconnected(). The per-queue purges are
 * individually guarded so they only reference queues that exist. */
void zmk_split_bt_purge_split_queues(void) {
    k_msgq_purge(&position_state_msgq);
#if IS_ENABLED(CONFIG_ZMK_INPUT_SPLIT)
    k_msgq_purge(&input_event_msgq);
#endif
#if ZMK_KEYMAP_HAS_SENSORS
    k_msgq_purge(&sensor_state_msgq);
#endif
}

static int service_init(void) {"""

SERVICE_OLD_NOTIFY = '            return bt_gatt_notify(NULL, &split_svc.attrs[i], &payload, sizeof(payload));'

SERVICE_NEW_NOTIFY = """            struct input_event_notify_item item = {.attr_index = (uint16_t)i, .payload = payload};
            int err = k_msgq_put(&input_event_msgq, &item, K_NO_WAIT);

            if (err == 0) {
                k_work_submit_to_queue(&service_work_q, &service_input_notify_work);
                return 0;
            } else {
                LOG_WRN("Input event queue full, dropping one and retry");

                struct input_event_notify_item discarded;
                k_msgq_get(&input_event_msgq, &discarded, K_NO_WAIT);
                err = k_msgq_put(&input_event_msgq, &item, K_NO_WAIT);

                if (err != 0) {
                    LOG_WRN("Failed to queue input event (%d)", err);
                    return err;
                }
            }"""

# ── service.c: is_connected guards on the three send_* entry points ──

SERVICE_OLD_REPORT_INPUT_START = """static int zmk_split_bt_report_input(uint8_t reg, uint8_t type, uint16_t code, int32_t value,
                                     bool sync) {

    for (size_t i = 0; i < split_svc.attr_count; i++) {"""

SERVICE_NEW_REPORT_INPUT_START = """static int zmk_split_bt_report_input(uint8_t reg, uint8_t type, uint16_t code, int32_t value,
                                     bool sync) {
    if (!zmk_split_bt_peripheral_is_connected()) {
        return -ENOTCONN;
    }

    for (size_t i = 0; i < split_svc.attr_count; i++) {"""

SERVICE_OLD_POS_STATE_START = """int send_position_state() {
    int err = k_msgq_put(&position_state_msgq, position_state, K_MSEC(100));"""

SERVICE_NEW_POS_STATE_START = """int send_position_state() {
    if (!zmk_split_bt_peripheral_is_connected()) {
        return -ENOTCONN;
    }
    int err = k_msgq_put(&position_state_msgq, position_state, K_MSEC(100));"""

SERVICE_OLD_SENSOR_STATE_START = """int send_sensor_state(struct sensor_event ev) {
    int err = k_msgq_put(&sensor_state_msgq, &ev, K_MSEC(100));"""

SERVICE_NEW_SENSOR_STATE_START = """int send_sensor_state(struct sensor_event ev) {
    if (!zmk_split_bt_peripheral_is_connected()) {
        return -ENOTCONN;
    }
    int err = k_msgq_put(&sensor_state_msgq, &ev, K_MSEC(100));"""

# ── service.h: declare the purge helper ──────────────────────────────

SERVICE_H_OLD = """int zmk_split_transport_peripheral_bt_report_event(
    const struct zmk_split_transport_peripheral_event *ev);"""

SERVICE_H_NEW = """int zmk_split_transport_peripheral_bt_report_event(
    const struct zmk_split_transport_peripheral_event *ev);

/* Drop all queued split peripheral notifications. Called by the BT
 * disconnected() handler to prevent post-disconnect -ENOTCONN floods. */
void zmk_split_bt_purge_split_queues(void);"""

# ── peripheral.c: call purge from disconnected() ─────────────────────

PERIPHERAL_OLD_DISCONNECTED = """static void disconnected(struct bt_conn *conn, uint8_t reason) {
    char addr[BT_ADDR_LE_STR_LEN];

    bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

    LOG_DBG("Disconnected from %s (reason 0x%02x)", addr, reason);

    is_connected = false;

    raise_zmk_split_peripheral_status_changed("""

PERIPHERAL_NEW_DISCONNECTED = """static void disconnected(struct bt_conn *conn, uint8_t reason) {
    char addr[BT_ADDR_LE_STR_LEN];

    bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

    LOG_DBG("Disconnected from %s (reason 0x%02x)", addr, reason);

    is_connected = false;

    /* Drop queued notifications so service_work_q doesn't drain them
     * against the now-dead link (prevents the -ENOTCONN flood observed
     * in trackpad-right-20260618-1234.log at bb594f2). */
    zmk_split_bt_purge_split_queues();

    raise_zmk_split_peripheral_status_changed("""

# ── Kconfig changes ─────────────────────────────────────────────────

KCONFIG_INSERT_MARKER = "\nendmenu\n\nendif # ZMK_SPLIT_BLE"

KCONFIG_INSERT_BLOCK = """\nif ZMK_INPUT_SPLIT

config ZMK_INPUT_SPLIT_MSG_QUEUE_SIZE
    int "Max number of input split messages to queue for sending over BLE"
    default 32
    help
      Sets the maximum number of input split events that can be queued for BLE notifications.
      Increasing this reduces drops during high-rate input bursts at the cost of additional RAM.

endif # ZMK_INPUT_SPLIT

endmenu

endif # ZMK_SPLIT_BLE"""


def _replace_once(src, old, new, label):
    """Replace `old` with `new` once. If `new` is already present, treat
    as already-applied and return unchanged."""
    if new in src:
        return src, False
    if old not in src:
        raise RuntimeError(f"Cannot find {label} patch marker in source")
    return src.replace(old, new, 1), True


def patch_service_c(path):
    with open(path, "r") as f:
        src = f.read()

    if "zmk_split_bt_purge_split_queues" in src:
        print(f"  service.c: already patched (v2 marker found)")
        return False

    if SERVICE_EARLY_MARKER not in src:
        raise RuntimeError(f"Cannot find early insertion marker (K_THREAD_STACK_DEFINE) in {path}")

    if SERVICE_INSERT_MARKER not in src:
        raise RuntimeError(f"Cannot find insertion marker in {path}")

    if SERVICE_OLD_NOTIFY not in src:
        raise RuntimeError(f"Cannot find old notify call in {path}")

    if SERVICE_PURGE_MARKER not in src:
        raise RuntimeError(f"Cannot find purge insertion marker (#endif + service_init) in {path}")

    # Block 1: forward declaration (unconditional, before all uses)
    src = src.replace(SERVICE_EARLY_MARKER, SERVICE_EARLY_BLOCK + SERVICE_EARLY_MARKER, 1)

    # Block 2: input queue + hardened callback (inside CONFIG_ZMK_INPUT_SPLIT)
    src = src.replace(SERVICE_INSERT_MARKER, SERVICE_INSERT_BLOCK + SERVICE_INSERT_MARKER, 1)
    src = src.replace(SERVICE_OLD_NOTIFY, SERVICE_NEW_NOTIFY, 1)

    # Block 3: purge helper (after #endif CONFIG_ZMK_INPUT_SPLIT, before service_init)
    src = src.replace(SERVICE_PURGE_MARKER, SERVICE_PURGE_REPLACEMENT, 1)

    # is_connected guards on the three send_* entry points
    src, _ = _replace_once(src, SERVICE_OLD_REPORT_INPUT_START,
                           SERVICE_NEW_REPORT_INPUT_START, "report_input guard")
    src, _ = _replace_once(src, SERVICE_OLD_POS_STATE_START,
                           SERVICE_NEW_POS_STATE_START, "send_position_state guard")
    src, _ = _replace_once(src, SERVICE_OLD_SENSOR_STATE_START,
                           SERVICE_NEW_SENSOR_STATE_START, "send_sensor_state guard")

    with open(path, "w") as f:
        f.write(src)

    print(f"  service.c: patched OK (v2)")
    return True


def patch_service_h(path):
    with open(path, "r") as f:
        src = f.read()

    if "zmk_split_bt_purge_split_queues" in src:
        print(f"  service.h: already patched")
        return False

    if SERVICE_H_OLD not in src:
        raise RuntimeError(f"Cannot find marker in {path}")

    src = src.replace(SERVICE_H_OLD, SERVICE_H_NEW, 1)

    with open(path, "w") as f:
        f.write(src)

    print(f"  service.h: patched OK")
    return True


def patch_peripheral_c(path):
    with open(path, "r") as f:
        src = f.read()

    if "zmk_split_bt_purge_split_queues" in src:
        print(f"  peripheral.c: already patched")
        return False

    if PERIPHERAL_OLD_DISCONNECTED not in src:
        raise RuntimeError(f"Cannot find disconnected() in {path}")

    src = src.replace(PERIPHERAL_OLD_DISCONNECTED, PERIPHERAL_NEW_DISCONNECTED, 1)

    with open(path, "w") as f:
        f.write(src)

    print(f"  peripheral.c: patched OK")
    return True


def patch_kconfig(path):
    with open(path, "r") as f:
        src = f.read()

    if "ZMK_INPUT_SPLIT_MSG_QUEUE_SIZE" in src:
        print(f"  Kconfig: already patched (ZMK_INPUT_SPLIT_MSG_QUEUE_SIZE found)")
        return False

    if KCONFIG_INSERT_MARKER not in src:
        raise RuntimeError(f"Cannot find insertion marker in {path}")

    src = src.replace(KCONFIG_INSERT_MARKER, KCONFIG_INSERT_BLOCK, 1)

    with open(path, "w") as f:
        f.write(src)

    print(f"  Kconfig: patched OK")
    return True


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <zmk_module_dir>")
        sys.exit(1)

    zmk_dir = sys.argv[1]
    files = {
        "service_c": os.path.join(zmk_dir, SERVICE_C_REL),
        "service_h": os.path.join(zmk_dir, SERVICE_H_REL),
        "peripheral_c": os.path.join(zmk_dir, PERIPHERAL_C_REL),
        "kconfig": os.path.join(zmk_dir, KCONFIG_REL),
    }

    print(f"Applying PR #3110 + v2 hardening to ZMK at {zmk_dir}")

    for label, f in files.items():
        if not os.path.exists(f):
            print(f"ERROR: {f} does not exist", file=sys.stderr)
            sys.exit(1)

    any_change = False
    any_change |= patch_service_c(files["service_c"])
    any_change |= patch_service_h(files["service_h"])
    any_change |= patch_peripheral_c(files["peripheral_c"])
    any_change |= patch_kconfig(files["kconfig"])

    if any_change:
        print("PR #3110 (+ v2 hardening) applied successfully.")
    else:
        print("PR #3110 (+ v2 hardening) already applied — no changes needed.")


if __name__ == "__main__":
    main()
