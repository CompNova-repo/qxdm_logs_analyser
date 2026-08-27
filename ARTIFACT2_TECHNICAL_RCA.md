# Technical Root-Cause Analysis — Recurring EPS Session Teardown with 5GS Registration Failure

| Field | Value |
|---|---|
| **Document ID** | RCA-QXDM-20230111-UE23-001 |
| **Log** | `UE23_TCP_UDP_UL_75_01-11.06-36-49-875.txt` (31.8 MB, QXDM ASCII decode) |
| **Index** | `qxdm_indexed_v2.db` — 42,162 events / 865 NAS records / 404 RF measurements |
| **UE** | UE23, Qualcomm modem, EN-DC capable, `Subscription ID = 1` |
| **Observation window** | 2023-01-11 11:17:29.011 – 11:35:47.717 (1,095 s / 18.3 min) |
| **PLMN** | MCC 302 / MNC 221; secondary 302/640 |
| **Verdict** | **5GC-side admission failure (5GMM cause #22) compounded by an invalid E-UTRAN redirect IE. Not an RF or mobility failure.** |
| **Reproducibility** | 20 / 20 cycles, byte-identical IEs |

### Normative references

- **3GPP TS 24.501** — 5GS NAS (5GMM causes, T3346, T3502)
- **3GPP TS 36.331** — E-UTRA RRC (`RRCConnectionRelease`, `RedirectedCarrierInfo`, `CarrierInfoNR-r15`)
- **3GPP TS 38.331** — NR RRC (`RRCSetup`, `RRCRelease`)
- **3GPP TS 38.104 §5.4.3** — NR synchronization raster / GSCN
- **3GPP TS 38.101-1** — NR FR1 operating bands
- **3GPP TS 38.133 §10.1.6** — SS-RSRP reporting range
- **3GPP TS 24.008 §10.5.7.4** — GPRS Timer 2 encoding

---

## 1. Executive technical statement

The UE executes a **network-commanded inter-RAT redirection from E-UTRAN to NR** 20 times. In every
instance the UE reaches **NR `RRC_CONNECTED`** and completes NAS security, then receives a
**5GMM `REGISTRATION REJECT` with cause `#22 (Congestion)`**. The AMF **omits the `T3346` back-off
timer**, so no congestion back-off is armed. The UE returns to E-UTRAN, performs a combined TA/LA
update, reconnects, and the eNB reissues the identical redirect — producing a sustained loop at a
mean period of 57.6 s.

Two independent, concurrent network defects are present:

- **DEF-1 (5GC, primary):** cause #22 issued without the `T3346` IE, contrary to TS 24.501
  §5.5.1.3.5. This is the loop driver.
- **DEF-2 (RAN, secondary):** `carrierFreq-r15 = 435000` (2175.000 MHz) is **off the FR1
  synchronization raster** and in a different band from the deployed NR carrier
  (126490 / 632.45 MHz). Masked by UE autonomous cell selection.

---

## 2. Verified measurement baseline — RF causes excluded

### 2.1 `qxdm_tool.py rf-summary` (n = 404)

```json
{ "records": 404,
  "avg_rsrp_dbm": -84.03, "min_rsrp_dbm": -90.0,   "max_rsrp_dbm": -75.641,
  "avg_rsrq_db":  -8.86,  "min_rsrq_db":  -10.422, "max_rsrq_db":  -7.0,
  "avg_rssi_dbm": -58.14, "min_rssi_dbm": -70.0,   "max_rssi_dbm": -54.0,
  "avg_snr_db":    27.44, "min_snr_db":    23.2,   "max_snr_db":    30.0 }
```

### 2.2 Per-RAT split

| RAT | n | RSRP avg (min/max) dBm | RSRQ avg dB |
|---|---|---|---|
| E-UTRA (0x1544) | 276 | −87.25 (−90.0 / −84.0) | −8.16 |
| NR (0xB97F) | 128 | **−77.08 (−79.352 / −75.641)** | −10.36 |
| NR serving PCI 323 only | 88 | **−77.10 (−79.352 / −75.641)** | −10.363 |

### 2.3 Reference `0xB97F` record @ 11:17:29.249 (49 ms before NR MIB decode)

```
Raster ARFCN = 126490      Num Cells = 1
|#  |PCI   |SFN |Beams|Cell Quality RSRP |Cell Quality RSRQ |
|  0|   323|   2|    1|          -78.359 |          -10.344 |
Detected Beams: SSB Index 0, RX Beam RSRP -80.508
```

### 2.4 L1/L2 integrity

| Indicator | Value | Source |
|---|---|---|
| NR5GMAC **DL BLER** | **0.0000 %** (n=20, zero non-zero samples) | 0x1FE8 |
| NR5GMAC **UL BLER** | **0.0000 %** (n=20, zero non-zero samples) | 0x1FE8 |
| `NR5GMAC_QSH_EVENT_RACH_SUCCESS` | **20 / 20**, on SSB index 0 | 0x1FE7 |
| RACH msg1/2 and msg3/4 failures | **0** (`msg12Fail: 0`, `msg34Fail: 0`) | 0x1FE7 |
| RLF / re-establishment / OOS | **0 occurrences** | full-log scan |
| NR `RRCRelease` abnormality flag | `isAbnormal - 0` | `nr5g_rrc_crp.c:4518` |

### 2.5 Cell-selection margin

NR SIB1 signals `q-RxLevMin -65` → **−130 dBm** threshold. Measured SS-RSRP of **−78.36 dBm**
exceeds the minimum by **51.6 dB**.

> **Conclusion:** RF, beam management, HARQ, and random access are all nominal. Signal-strength,
> coverage, interference, and mobility hypotheses are **excluded by measurement**.

---

## 3. Primary defect (DEF-1) — 5GMM cause #22 without T3346

### 3.1 `qxdm_tool.py nas` — 20 records, 100 % identical

```json
{ "timestamp": "2023 Jan 11  11:17:29.602",
  "rat": "5GNR",
  "msg": "NR5G NAS MM5G Plain OTA Incoming Msg  --  Registration reject",
  "cause_code": 22, "cause_str": "Congestion",
  "t3346_timer": null, "t3502_timer": 720 }
```

Aggregate: `20 × (22, 'Congestion', t3346=None, t3502=720 s)`.
First 11:17:29.602 → last 11:35:44.645.

### 3.2 Decoded `0xB80A` PDU (11:17:29.602, ASN.1/CSN.1 fields verbatim)

```
NR5G NAS MM5G Plain OTA Incoming Msg  --  Registration reject
  ext_protocol_disc = 126 (0x7e)        # 5GS Mobility Management
  security_header   = 0 (0x0)           # plain, not integrity-protected
  msg_type          = 68 (0x44)         # REGISTRATION REJECT
  registration_reject
    _5gmm_cause     = 22 (0x16) (Congestion)
    t3346_incl      = 0 (0x0)           # <-- DEFECT: back-off timer ABSENT
    t3502_incl      = 1 (0x1)
    t3502  { length = 1, unit = 1, timer_2_value = 12 }   # 12 min = 720 s
    eap_msg_incl = 0 | rej_nssai_incl = 0 | cag_info_list_incl = 0
```

Corroborating QTrace: `mm5g_utility.c:646 | MM_QSH_EVENT_NAS_OTA_MSG |
event_data=0x7E440016 | NR5G DL REGISTRATION_REJECT, EPD 126 MSG ID 68 cause 22`.

### 3.3 Specification violation

**TS 24.501 §5.5.1.3.5** — on `REGISTRATION REJECT` with cause **#22 (Congestion)**, the network
**shall** include the **T3346** value IE, and the UE starts T3346 and refrains from further
registration attempts in the PLMN until it expires.

`t3346_incl = 0` in **20/20** rejections. The UE therefore has **no armed back-off**.

**`T3502 = 720 s` does not substitute.** Per TS 24.501 §5.5.1.2.7 / §5.5.1.3.7, T3502 governs
re-initiation after the *registration attempt counter* reaches 5 — a different trigger. It does not
gate the per-attempt retry rate, and empirically did not:

```
Inter-attempt intervals (s), n=19:
[62.8, 137.4, 27.6, 60.2, 62.6, 39.2, 34.0, 68.3, 52.1, 60.2,
  45.1, 60.9, 60.1, 75.8, 47.7, 31.3, 115.2, 31.1, 23.4]
mean 57.6 · min 23.4 · max 137.4    (never approaches 720 s)
```

`rej_nssai_incl = 0` ⇒ AMF-wide congestion, **not** S-NSSAI/slice-specific.
`security_header = 0` ⇒ rejection sent unprotected, so a false-base-station scenario cannot be
excluded from this log alone (see §7).

---

## 4. Secondary defect (DEF-2) — invalid `RedirectedCarrierInfo`

### 4.1 `RRCConnectionRelease` IE, identical in all 20 events (TS 36.331)

```asn1
value DL-DCCH-Message ::= {
  message c1 : rrcConnectionRelease : {
    rrc-TransactionIdentifier 3,
    criticalExtensions c1 : rrcConnectionRelease-r8 : {
      releaseCause              other,
      redirectedCarrierInfo nr-r15 : {
        carrierFreq-r15           435000,
        subcarrierSpacingSSB-r15  kHz15
      }
    }
  }
}
```

Header: `Radio Bearer ID = 1, Physical Cell ID = 49, Freq = 900, Msg Length = 8`.
**`multiFrequencyBandListNR-r15` is ABSENT** — the UE receives no band indication for the target.

Release inventory: **20** with `redirectedCarrierInfo`, **18** bare (`releaseCause other`, len 2).

### 4.2 Frequency validation

`ARFCN-ValueNR 435000`, FR1 ΔF_Global = 5 kHz (TS 38.104 §5.4.2.1):

```
F_REF = 5 kHz × 435000 = 2,175,000 kHz = 2175.000 MHz
```

**Synchronization-raster test** (TS 38.104 §5.4.3.1, FR1: `SS_REF = N×1200 kHz + M×50 kHz`,
N = 1…2499, M ∈ {1,3,5}):

```
Exhaustive search over all (N, M) for 2,175,000 kHz  ->  NO SOLUTION
  N=1812 -> 2,174,400 kHz, residual 600 kHz -> requires M=12  (invalid)
  N=1811 -> 2,173,200 kHz, residual 1800 kHz -> requires M=36 (invalid)
Nearest valid SSB positions: 2174.45 / 2174.55 / 2174.65 / 2175.65 MHz
```

⇒ **2175.000 MHz is off-raster. No SSB can exist at the commanded position.**

Control — the carrier actually in service:

```
126490 × 5 kHz = 632,450 kHz = 632.45 MHz
  -> N=527, M=1  ->  GSCN = 3N + (M-3)/2 = 1580   VALID
  -> Band n71 (DL 617–652 MHz, TS 38.101-1)
```

**Band inconsistency:** 2175.000 MHz lies outside n1 (2110–2170 MHz) and falls in n66
(2110–2200 MHz, AWS-3). The deployed carrier is **n71**. With `multiFrequencyBandListNR-r15`
absent, the UE cannot disambiguate the intended band. Additionally `subcarrierSpacingSSB-r15 kHz15`
is inconsistent with typical n66 SSB deployment (30 kHz), while matching the actual n71 cell.

### 4.3 Empirical confirmation the target is never acquired

| Check | Result |
|---|---|
| Occurrences of `435000` in the 31.8 MB log | **20 — exclusively on the `carrierFreq-r15` line** |
| `0xB821` NR RRC OTA packets referencing 435000 | **0** |
| `0xB97F` searcher records for ARFCN 435000 | **0** |
| Distinct `Freq` in all 402 `0xB821` headers | **`126490` only** |
| Distinct `arfcn` in NR `rf_kpis` | **`126490` only** |

⇒ The commanded carrier produces **zero** searcher, measurement, or acquisition activity. The UE
recovers via **autonomous NR cell selection** onto n71.

---

## 5. Cycle 1 reference timeline (11:17:29.011 → 11:17:33.317)

| Timestamp | Seq | Layer | Event / IE |
|---|---|---|---|
| 11:17:29.011 | 1294 | LTE RRC | `RRCConnectionRelease`, `releaseCause other`, `redirectedCarrierInfo nr-r15 {435000, kHz15}` |
| 11:17:29.011 | 1218 | LTE RRC | `EVENT_LTE_RRC_DL_MSG` — DL DCCH, RRC Connection Release |
| 11:17:29.011 | 1221/1222 | LTE RRC | `RRC State = Closing`; trigger `CLOSING_TRIGGER_OTHER` |
| 11:17:29.011 | — | LTE RRC | Timer `Connect Rel` = 60, Start |
| 11:17:29.036 | 1234/1237/1240 | LTE RRC | 3 × SCell → `Not Configured` (EARFCN 2850 / 3350 / 66811) |
| 11:17:29.050 | 1251 | LTE MAC | `EVENT_LTE_MAC_RESET_V2`, `CAUSE = Connection release` |
| 11:17:29.061 | 1352 | LTE RRC | `LRRC_QSH_EVENT_REDIR` `event_data=0x3090` — state 0, RAT 9→12, UE triggered: 0 |
| **11:17:29.186** | **1358** | **NR RRC** | **`NR5GRRC_QSH_EVENT_REDIR … STATUS FAILURE` (`0x46`, `nr5g_rrc_irat_from_lte_mgr.c:5522`) — commanded carrier 435000** |
| 11:17:29.249 | — | NR ML1 | `0xB97F` — Raster ARFCN **126490**, PCI 323, RSRP −78.359, RSRQ −10.344 |
| 11:17:29.252 | 1401 | NR RRC | MIB decoded, PCI 323 — `scs15or60`, `ssb-SubcarrierOffset 6`, `cellBarred notBarred` |
| 11:17:29.374 | 1408 | NR RRC | SIB1 — `q-RxLevMin -65`, PLMN 302/221 + 302/640, TAC `0x0091B6` (37302) |
| 11:17:29.387 | 1423 | NR RRC | `REDIR … STATUS SUCCESS` (`0x45`, same file **:5496**) — the *autonomously selected* cell |
| 11:17:29.388 | 1390 | LTE RRC | `EVENT_LTE_RRC_IRAT_REDIR_FROM_EUTRAN_END` — `RAT = NR5G, Status Cause = Success` ← **masks DEF-2** |
| 11:17:29.391 | 1443/1472 | NR RRC/NAS | `RRCSetupRequest`; `REGISTRATION_REQUEST` (mob reg updating); T3510 start |
| 11:17:29.391 | 1446 | NR MAC | `RACH_TRIG` — `Access_Reason: CONNECTION_REQ`, `Cont_Type: CB` |
| 11:17:29.461–.485 | 1453–1463 | NR MAC | msg1 (attempt #0) → msg2 `RAID_MATCH` → msg3 → **`RACH_SUCCESS` on SSB ID 0** |
| 11:17:29.486 | 1467 | NR RRC | `RRCSetup` (TS 38.331) |
| 11:17:29.515 | 1542/1546 | NR RRC | `RRCSetupComplete` → **`RRC STATE - CONNECTED`** |
| 11:17:29.572 | 1566/1570 | NR NAS | `SECURITY_MODE_COMMAND` → `SECURITY_MODE_COMPLETE` |
| **11:17:29.602** | **—** | **NR NAS** | **`REGISTRATION REJECT` — `_5gmm_cause = 22 (Congestion)`, `t3346_incl = 0`, `t3502 = 720 s`** |
| 11:17:29.602 | 1588 | NR RRC | `RRCRelease`, `criticalExtensions rrcRelease { }` — no `deprioritisationReq`, no redirect |
| 11:17:29.616 | 1652 | NR MAC | MAC reset, cause `CONNECTION_RELEASE` |
| 11:17:29.658 | 1662 | NR RRC | `CONN_REL_CFG_REL`, conn mode 2, **`isAbnormal - 0`** |
| 11:17:29.659 → .670 | 1665/1682 | NR RRC | `IDLE_NOT_CAMPED` → `IDLE` |
| 11:17:33.064 | 1732 | LTE RRC | `RRC State = IRAT To LTE Started` |
| 11:17:33.179 | 1753 | LTE RRC | `Idle Camped`, trigger `CAMPED_TRIGGER_OTHER` |
| 11:17:33.186 | 1762 | LTE NAS | `EVENT_NAS_TAU` — `COMBINED_TA_LA_UPDATING`, Start |
| 11:17:33.317 | 1913 | LTE NAS | `EVENT_NAS_TAU` — End, **`end reason = Accept`** |
| 11:17:33.316 | 1876 | LTE RRC | `RRCConnectionRelease` (bare, `releaseCause other`) — cycle closes |

**E-UTRAN context:** PCell PCI 49 / EARFCN 900 (Band 2); SCells EARFCN 2850, 3350, 66811 (Band 66) — 4CC CA.

---

## 6. Loop mechanics and quantified impact

```
     ┌───────────────────────────────────────────────────────────────┐
     │  E-UTRAN connected, 4CC CA, RSRP −87 dBm, SNR 27 dB           │
     └──────────────────────────────┬────────────────────────────────┘
        eNB: RRCConnectionRelease + redirectedCarrierInfo{435000}
                                    ▼
        DEF-2: 435000 off-raster → REDIR STATUS FAILURE (+175 ms)
                                    ▼
        UE autonomous NR cell selection → n71 / 126490 / PCI 323
        SS-RSRP −78.4 dBm · RACH 1st try · RRC_CONNECTED · security OK
                                    ▼
        DEF-1: REGISTRATION REJECT #22 Congestion, t3346_incl = 0
                                    ▼
        RRCRelease → IDLE_NOT_CAMPED → return to E-UTRAN → TAU Accept
                                    │
                └───────────── no back-off armed ──────────────┘
                        mean 57.6 s → repeat ×20
```

**Why the loop is stable:** (a) the AMF refuses admission; (b) `t3346_incl = 0` leaves the UE with
no obligation to defer; (c) the 5GMM rejection is **invisible to E-UTRAN**, so the eNB's redirect
policy reissues the identical IE on every reconnection. No element retains failure state.

| Metric | Value |
|---|---|
| NR registration success rate | **0 % (0/20)** |
| Teardown rate | 1.10 / min (20 in 1,095 s) |
| Recovery per cycle (release → `Idle Camped`) | 3.49 – 4.33 s, mean **3.96 s** |
| Cumulative disruption | **79.2 s = 7.2 %** of window |
| SCell teardown/rebuild events | 60 (3 per cycle) |

---

## 7. Interpretation guidance — two misleading artifacts

1. **`Cause = PCell RLF/Connection Restablishement` on all 60 SCell transitions is NOT an RLF.**
   Corroborants: `vRLF Cause = vRLF cause invalid`, `EVENT_LTE_MAC_RESET_V2 CAUSE = Connection
   release`, `isAbnormal - 0`, and **zero** RLF/re-establishment records log-wide. This is the
   normal release code path with a misleading enum label. Do not open an RLF investigation.

2. **`REDIR` reports both FAILURE and SUCCESS per cycle** — `0x46` from
   `nr5g_rrc_irat_from_lte_mgr.c:5522` (commanded carrier 435000) and `0x45` from **:5496**
   (autonomously selected 126490). Only the second propagates to
   `EVENT_LTE_RRC_IRAT_REDIR_FROM_EUTRAN_END`, so **E-UTRAN KPIs record redirection as 100 %
   successful while the commanded carrier fails 100 % of the time.** Any RAN dashboard built on
   that event is blind to DEF-2.

3. **Security caveat.** The rejection arrives with `security_header = 0` (plain, no integrity
   protection) before any 5G security context exists. TS 24.501 §4.4.4.2 permits this, but a plain
   `REGISTRATION REJECT` is also the signature of a false-base-station downgrade. The consistent
   TAC/PLMN/PCI and successful RACH make genuine AMF congestion the strongly favoured explanation;
   confirm against AMF-side counters (§8, CORE-2) before closing.

---

## 8. Required actions

### Core / 5GC — owner of the primary defect

| ID | Action | Verification |
|---|---|---|
| **CORE-1** | Populate the **T3346** IE on every `REGISTRATION REJECT` with cause #22, per TS 24.501 §5.5.1.3.5. **Highest leverage; independent of capacity remediation.** | Re-trace shows `t3346_incl = 1`; inter-attempt interval ≥ T3346 |
| **CORE-2** | Investigate AMF admission-control/capacity for **TAC 37302**, PLMN 302/221. `rej_nssai_incl = 0` ⇒ AMF-wide, not slice-scoped. Cross-check registration-reject counters and NGAP load for 11:17–11:36 UTC. | Cause #22 rate → 0; registration success restored |
| **CORE-3** | Confirm the rejections originate from the production AMF and not an unauthorized gNB (§7.3). | AMF logs correlate 1:1 with the 20 UE-side rejects |
| **CORE-4** | Alarm on *"cause #22 emitted with `t3346_incl = 0`"*. | Synthetic reject raises the alarm |

### RAN — owner of the secondary defect

| ID | Action | Verification |
|---|---|---|
| **RAN-1** | Correct `carrierFreq-r15`: **435000 → 126490** (n71, GSCN 1580). Audit every eNB using the 2175.000 MHz value. | `0xB821` activity on the commanded ARFCN; no `0x46` REDIR failure |
| **RAN-2** | Populate **`multiFrequencyBandListNR-r15`** so the UE can resolve the target band. | IE present in `RRCConnectionRelease` |
| **RAN-3** | Validate every provisioned `ARFCN-ValueNR` against the TS 38.104 §5.4.3 sync raster in config CI. Confirm `subcarrierSpacingSSB-r15` matches the deployed SSB SCS. | Off-raster values rejected at provisioning |
| **RAN-4** | Suppress NR redirect for a UE that has just been NR-refused (timer or counter based). | No re-redirect within the hold-off period |
| **RAN-5** | Stop deriving redirect-success KPIs solely from `EVENT_LTE_RRC_IRAT_REDIR_FROM_EUTRAN_END` (§7.2). | KPI reflects commanded-carrier outcome |

---

## 9. Diagnostic tooling defect — found and corrected

The evidence in §3 was **absent from the original index**, so the dataset pointed exclusively at the
radio. Three defects in `indexer.py`:

1. **Case-mismatched message-code comparison (critical).**
   `code_str = str(current_code).upper()` yields `"0XB80A"`, compared against literals
   `"0xB80A"`, `"0xB0EC"`, `"0xB0ED"`, `"0xB97F"`, `"0x1FFB"`, `"0xB821"`, `"0xB0C0"` — all with a
   lowercase `x`. **These never match.** NAS capture survived only via the fallback
   `"5GMM" in block_text or "EMM" in block_text`, which is case-sensitive; the reject PDU contains
   `_5gmm_cause` in lowercase and no uppercase `5GMM`/`EMM` token. **All 20 rejects were silently
   discarded** (`nas_events` held 747 rows, of which **0** had a cause code).
   *Fix:* upper-cased literals + token-boundary case-insensitive fallback.

2. **Cause string captured the hex restatement.**
   `(?:\s*\((.*?)\))?` bound to the first parenthetical, yielding `"0x16"` instead of
   `"Congestion"`. *Fix:* explicitly consume the optional `(0x..)` group.

3. **`0xB97F` RSRP never parsed; timer fields stored a boolean.**
   The searcher emits an ASCII table, not `key = value`, so the `rsrp\s*=` regex never matched.
   Timer columns stored `t3346_incl` (0/1) rather than a duration. *Fixes:* table-row parser with a
   TS 38.133 §10.1.6 sanity filter (−156…−31 dBm, which also removes 20 zero-filled placeholder
   rows), and a TS 24.008 §10.5.7.4 GPRS-Timer-2 decoder writing **seconds**, `NULL` when the IE is
   absent.

**Result** (`qxdm_indexed.db` 747 NAS / 316 RF → `qxdm_indexed_v2.db` 865 / 404):
`qxdm_tool.py nas` now returns the 20 rejects with `cause_code 22`, `cause_str "Congestion"`,
`t3346_timer null`, `t3502_timer 720`.

### Residual limitation

**BLER is still not indexed.** `0x1FE8` QTrace MAC-metric blocks are captured only when the block
contains `"RRC"`, so the `BLER.%` records are dropped; `rf_kpis` also has no `bler` column. The §2.4
BLER figures were sourced directly from the raw log. Recommend adding a `bler` column and an
explicit `0x1FE8` MAC-metrics branch.

**Note:** `rf-summary` aggregates E-UTRA and NR into one population (§2.1). Because the RATs differ
by ~10 dB in RSRP, the combined mean is not physically meaningful — always use the §2.2 per-RAT
split. Recommend a `--rat` filter.

---

## 10. Conclusion

Recurring session teardown is caused by a **5G core admission failure**, not by the radio access
network or radio conditions. The AMF rejects every registration with **5GMM cause #22 (Congestion)**
while **omitting the mandatory `T3346` back-off timer**, which converts a single admission failure
into a sustained 18-minute retry loop that repeatedly destroys a healthy 4-carrier E-UTRA
aggregation. A concurrent RAN misconfiguration directs the UE to an **off-raster, wrong-band NR
frequency (2175.000 MHz vs. the deployed 632.45 MHz)**; this is masked by UE autonomous cell
selection and is therefore invisible to existing RAN KPIs.

**`CORE-1` (restore the T3346 IE) is the single highest-leverage fix** — it terminates the loop even
while the congestion condition persists, and removes the retry amplification that aggravates it.

**Evidence quality:** 20/20 reproduction with byte-identical IEs; RF causes excluded by 404
measurements, 0.0000 % BLER, 20/20 first-attempt RACH success, and zero RLF events.
