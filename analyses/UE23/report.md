# UE23: recurring 5G registration rejection loop

**Test:** `UE23` (TCP/UDP UL throughput, 2023-01-11 11:17:27 to 11:37:25)
**Source:** `UE23_TCP_UDP_UL_75_01-11_06-36-49-875.txt`, 1 chunk
**Index:** 30,756 events, 20 NAS records, 404 RF measurements
**Verdict:** network-side defects (5G core primary, RAN secondary). RF is excluded.

Every finding below is backed by `qxdm_tool.py` output captured and stored with this analysis in the results DB.

## 1. RF is not the cause

`rf-summary`: average RSRP -84.03 dBm (minimum -90.0), average RSRQ -8.86 dB (minimum -10.42), average SNR 27.44 dB (minimum 23.2). Radio conditions stay good for the whole capture.

## 2. DEF-1 (5G core, primary): cause #22 without T3346

All 20 NR registration attempts are rejected with 5GMM cause #22 "Congestion" (`nas`). None carries T3346 (`t3346_timer: null` in all 20; the reject block at sequence 1590 shows `t3346_incl = 0`). T3502 is included and decodes to 720 s (unit 1, value 12).

The requests are mobility registration updates (`_5gs_reg_type = 2`), so TS 24.501 §5.5.1.3.5 applies: when the network rejects for congestion control it sets cause #22 and assigns a back-off timer T3346. Without T3346 the UE has no congestion back-off and keeps retrying.

Rejects arrive every 57.6 s on average (minimum 23.4 s, maximum 137.5 s, standard deviation 28.8 s) across 1,095 s. There are no successful registrations.

## 3. DEF-2 (RAN, secondary): LTE to NR redirect to an unconfigured carrier

Each cycle starts with an LTE `RRCConnectionRelease` whose `redirectedCarrierInfo` points to NR-ARFCN **435000** (20 of 20 redirects, `anomalies --with-cause`).

The same LTE network's measurement configuration (sequence 4728) lists NR carriers 632448, **434890**, **434910**, 126490 and 639168. 435000 is not among them. The two nearest, 434890 and 434910, are 2174.45 MHz and 2174.55 MHz. Read as an SSB position, 435000 = 2175.000 MHz is not on the NR synchronisation raster for this range (N x 1.2 MHz + 50/150/250 kHz, TS 38.104 §5.4.3), while 434890 and 434910 are. This looks like a mistyped redirect target rather than a missing cell. The UE recovers by reselecting NR on its own, which masks the problem.

Confidence is medium: the raster argument assumes `carrierFreq-r15` carries the SSB ARFCN, as defined for `CarrierInfoNR-r15` in TS 36.331.

## 4. Why T3502 never stops the loop (hypothesis)

T3502 is the wait after five failed registration attempts (TS 24.501 §5.5.1.3.7). It is not a periodic timer; the periodic registration timer is T3512 (T3412 in LTE). If it applied here, the UE would pause for 720 s. The largest gap observed is 137.5 s, so it never does. The likely reason is that each cycle's successful LTE tracking area update resets the attempt counter before it reaches five. This needs confirming against the UE behaviour; it explains why the loop is self-sustaining.

## 5. Tooling gaps found during this analysis

- The index does not claim LTE NAS EMM messages (`0xB0ED` tracking area update request, `0xB0EC` accept) or NR registration requests (`0xB80B`). They appear in `unknown-types`. Message counts for these, and the registration type above, had to come from the raw log.
- `window <timestamp>` anchors on the first event in that millisecond. Around 100 events share 11:17:29.602, so the reject block was unreachable. Added `window --sequence N` and `sequence` in `nas` output to fix this.

## Corrections to the earlier report (2026-10-26)

1. T3502 was described as the periodic tracking area timer. It is the retry wait after five failed attempts (see section 4).
2. The "timer decoding risk" was a false alarm. `decode_gprs_timer2` matches TS 24.008 §10.5.7.4: unit 2 = 6 minutes, units 3 to 6 = 1 minute.
3. DEF-2 was framed as "1,543 MHz away from the only NR carrier". The network configures several NR carriers; 435000 matches none of them.
4. "σ range 23.4 to 137.5 s" was the minimum and maximum. The standard deviation is 28.8 s.
5. The old index's 845 extra NAS rows were EMM timer events, QMI packets and QTrace blocks caught by the over-matching fixed in `fix/issue#1`, not tracking area or service requests. The 11,406 extra events are 11,000 QTrace `0x1FE7` blocks plus 406 others.
6. The report said it used only `qxdm_tool` output, but quoted counts and fields that are not in the index (see section 5).

## Recommendations

| # | Action | Owner | Priority |
|---|---|---|---|
| 1 | Include T3346 in Registration Reject cause #22 | Core / AMF | P1 |
| 2 | Correct the redirect target to the intended NR carrier (434890 or 434910 if n66 is intended, or 126490) or drop the redirect | RAN | P2 |
| 3 | Confirm why T3502 is never applied (attempt counter reset by LTE TAU) | Device / Core | P3 |
| 4 | Index LTE EMM and NR registration request messages | Tooling | P3 |
