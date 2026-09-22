# -*- coding: utf-8 -*-
"""通用 HID 报告描述符解析器：解出 (report_id, 类型, 位偏移, 位宽, usage 路径, 逻辑范围)"""
import sys

W = 0x84  # Power Device page
BS = 0x85  # Battery System page

PAGES = {0x84: "PowerDevice", 0x85: "BatterySystem", 0x01: "GenericDesktop"}

UP84 = {
    0x02: "PresentStatus", 0x04: "UPS", 0x12: "AwaitingPower", 0x1A: "PowerSummary?",
    0x1C: "Test?", 0x21: "CommunicationLost", 0x24: "BatterySystem", 0x25: "Battery?",
    0x28: "BatteryId", 0x29: "BatterySupported", 0x2C: "RemainingCapacityLimit",
    0x2D: "RemainingCapacityLimitSetting", 0x2E: "CapacityMode", 0x30: "RemainingCapacity?",
    0x31: "RunTimeToEmpty?", 0x32: "EstimatedTimeToFull?", 0x33: "DischargeTime?",
    0x36: "Voltage?", 0x40: "Charger", 0x41: "ChargerConnState?", 0x42: "Charging",
    0x43: "Discharging", 0x44: "FullyCharged", 0x45: "FullyDischarged",
    0x4B: "NeedReplacement", 0x4D: "RemainingCapacityWarning?", 0x50: "DelayBeforeShutdown?",
    0xD0: "VendorD0", 0xD1: "VendorD1", 0x1F: "U1F", 0x02: "PresentStatus",
}
UP85 = {
    0x2C: "B2C", 0x66: "Voltage", 0x68: "Current", 0x69: "ChargePercent?",
    0x6C: "Temperature?", 0x83: "B83", 0x89: "B89", 0x8F: "B8F", 0x29: "BatterySupported",
    0x65: "B65",
}


def usage_name(page, uid):
    if page == 0x84:
        return UP84.get(uid, "U%02X" % uid)
    if page == 0x85:
        return UP85.get(uid, "B%02X" % uid)
    return "P%X_%02X" % (page, uid)


def parse(data):
    fields = []
    usage_page = 0
    log_min = log_max = 0
    exp = 0
    unit = 0
    rsize = 0
    rid = 0
    rcount = 0
    usages = []           # local stack of (page, id) or ("min", (p,a,b))
    usagemin = None
    usagemax = None
    stack = []            # collection path
    gpush = []

    i = 0
    col_stack = []        # (usage) for path
    n = 0
    while i < len(data):
        h = data[i]; i += 1
        if h == 0xFE:  # long item
            if i + 2 > len(data): break
            sz = data[i]; tag = data[i+1]; i += 2 + sz
            continue
        sz = h & 3
        btype = (h >> 2) & 3
        btag = (h >> 4) & 15
        if sz < 3:
            nbytes = (0, 1, 2, 4)[sz]
            raw = int.from_bytes(data[i:i + nbytes], "little")
            # LogicalMin/Max、Physical 带符号；其余（UsagePage/Usage/Size/Count/ID/Unit）无符号
            if btype == 1 and btag in (1, 2, 3, 4) and (raw >> (nbytes * 8 - 1)):
                val = raw - (1 << (nbytes * 8))
            else:
                val = raw
            i += nbytes
        else:
            val = None
            i += 4

        if btype == 0:  # main
            if btag == 0xA:  # collection
                u = usages[-1] if usages else (0, 0)
                col_stack.append(u)
                usages = []
            elif btag == 0xC:  # end collection
                if col_stack: col_stack.pop()
                usages = []
            elif btag in (0x8, 0x9, 0xB):  # input/output/feature
                typ = {0x8: "INPUT", 0x9: "OUTPUT", 0xB: "FEATURE"}[btag]
                cnt = rcount if rcount else 1
                flags = val & 0xFF if val is not None else 0
                # expand usages
                uu = list(usages)
                if usagemin is not None and usagemax is not None:
                    uu = [(usagemin[0], x) for x in range(usagemin[1], usagemax[1] + 1)]
                if not uu:
                    uu = [(usage_page, 0)]
                # pad usage list to count
                while len(uu) < cnt:
                    uu.append(uu[-1])
                idx = 0
                for k in range(cnt):
                    u = uu[idx] if flags & 0x2 == 0 else uu[0]  # array uses one usage
                    if flags & 0x2:
                        idx = 0
                    else:
                        idx = min(idx + 1, len(uu) - 1)
                    path = "/".join(usage_name(p, uid) for p, uid in col_stack + [u])
                    fields.append(dict(rid=rid, typ=typ, bit=n, size=rsize, path=path,
                                       flags=flags, lmin=log_min, lmax=log_max, exp=exp, unit=unit))
                    n += rsize
                usages = []
                usagemin = usagemax = None
            continue

        if btype == 1:  # global
            if btag == 0x0: usage_page = val
            elif btag == 0x1: log_min = val
            elif btag == 0x2: log_max = val
            elif btag == 0x5: exp = val
            elif btag == 0x6: unit = val
            elif btag == 0x7: rsize = val
            elif btag == 0x8:
                rid = val; n = 0
            elif btag == 0x9: rcount = val
            elif btag == 0xB: gpush.append((usage_page, log_min, log_max, exp, unit, rsize, rid, rcount))
            elif btag == 0xC:
                if gpush:
                    (usage_page, log_min, log_max, exp, unit, rsize, rid, rcount) = gpush.pop()
        elif btype == 2:  # local
            if btag == 0x0:
                if val is not None:
                    pg = (val >> 16) & 0xFFFF
                    uid = val & 0xFFFF
                    usages.append((pg if pg else usage_page, uid))
            elif btag == 0x1:
                usagemin = (usage_page, val)
            elif btag == 0x2:
                usagemax = (usage_page, val)
    return fields


def main():
    hx = sys.argv[1].strip()
    data = bytes.fromhex(hx)
    for f in parse(data):
        if f["flags"] & 1 and f["flags"] & 2 and f["flags"] & 4:  # const+array → 纯 padding
            kind = "pad"
        else:
            kind = ""
        print("rid=%d %-7s bit=%3d size=%2d %-45s lmin=%s lmax=%s exp=%s %s" % (
            f["rid"], f["typ"], f["bit"], f["size"], f["path"], f["lmin"], f["lmax"], f["exp"], kind))


if __name__ == "__main__":
    main()
