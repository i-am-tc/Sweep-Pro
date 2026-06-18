#!/usr/bin/env python3
"""
Apply ZMK PR #3110 out-of-tree: queue input events over BLE.

Replaces synchronous bt_gatt_notify() in the peripheral input-split path
with a K_MSGQ + service_work_q pattern (matching how position and sensor
events already work).

Usage: python3 apply_pr3110.py <zmk_module_dir>
   e.g. python3 apply_pr3110.py /tmp/zmk-config/zmk
"""

import sys
import os

SERVICE_C_REL = "app/src/split/bluetooth/service.c"
KCONFIG_REL = "app/src/split/bluetooth/Kconfig"

# ── service.c changes ───────────────────────────────────────────────

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
            LOG_ERR("Error notifying input event %d", err);
        }
    }
}

static K_WORK_DEFINE(service_input_notify_work, send_input_event_callback);

"""

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


def patch_service_c(path):
    with open(path, "r") as f:
        src = f.read()

    if "input_event_msgq" in src:
        print(f"  service.c: already patched (input_event_msgq found)")
        return False

    if SERVICE_INSERT_MARKER not in src:
        raise RuntimeError(f"Cannot find insertion marker in {path}")

    if SERVICE_OLD_NOTIFY not in src:
        raise RuntimeError(f"Cannot find old notify call in {path}")

    src = src.replace(SERVICE_INSERT_MARKER, SERVICE_INSERT_BLOCK + SERVICE_INSERT_MARKER, 1)
    src = src.replace(SERVICE_OLD_NOTIFY, SERVICE_NEW_NOTIFY, 1)

    with open(path, "w") as f:
        f.write(src)

    print(f"  service.c: patched OK")
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
    service_c = os.path.join(zmk_dir, SERVICE_C_REL)
    kconfig = os.path.join(zmk_dir, KCONFIG_REL)

    print(f"Applying PR #3110 to ZMK at {zmk_dir}")

    for f in (service_c, kconfig):
        if not os.path.exists(f):
            print(f"ERROR: {f} does not exist", file=sys.stderr)
            sys.exit(1)

    service_patched = patch_service_c(service_c)
    kconfig_patched = patch_kconfig(kconfig)

    if service_patched or kconfig_patched:
        print("PR #3110 applied successfully.")
    else:
        print("PR #3110 already applied — no changes needed.")


if __name__ == "__main__":
    main()
