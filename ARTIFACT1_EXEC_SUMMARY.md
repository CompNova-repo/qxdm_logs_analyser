# 5G Session Teardown Incident — Executive Summary

**Log:** `UE23_TCP_UDP_UL_75_01-11.06-36-49-875.txt` (UE23, TCP/UDP uplink throughput test)
**Window:** 2023-01-11 11:17:29 – 11:35:44 UTC (18.3 min) · **Network:** MCC 302 / MNC 221 (+302/640), TAC 37302
**Classification:** P1 — Total 5G service denial · **Owner:** Core (5GC) — primary · RAN — secondary
**Evidence base:** 42,162 indexed events · 20/20 failures reproduced identically

---

## Slide 1 — What Happened

**5G is completely unavailable on this device, and the 5G core is the reason. Signal quality is excellent and is not a factor.**

The device attempted to move from 4G to 5G **20 times in 18 minutes**. Every single attempt was
**refused by the 5G core network**, not by the radio.

Each attempt got remarkably far before being rejected:

| Stage | Result |
|---|---|
| Find and lock onto the 5G cell | ✅ Succeeded 20/20 |
| Establish the radio connection | ✅ Succeeded 20/20, first try, zero retries |
| Complete security/encryption setup | ✅ Succeeded 20/20 |
| **Register on the 5G core network** | ❌ **Rejected 20/20 — "Congestion"** |

> The device proved it had a perfectly good 5G radio link every time, then was turned away
> at the final step by the core network.

**Two independent defects, both on the network side:**

1. **The 5G core refuses every registration** and cites congestion — but omits the industry-standard
   "wait before retrying" instruction. The device therefore retries almost immediately, forever.
2. **The 4G network points the device at a 5G frequency that does not exist.** The device only
   recovers because it independently finds the real 5G cell on its own.

---

## Slide 2 — Root Cause

### Primary — 5G Core rejects with congestion, then omits the back-off timer

The 5G core returned the same rejection **20 out of 20 times**: cause code **#22 "Congestion"**.

Critically, it **left out the `T3346` back-off timer** that the 3GPP standard requires with this
rejection. That timer is what tells a device "we're busy — stand down for a while."

**Consequence:** with no instruction to wait, the device retries every **~58 seconds** instead of
backing off. One rejection becomes an **18-minute continuous failure loop**. The missing timer is
what turns a capacity problem into a customer-visible service outage.

### Secondary — 4G hands out a non-existent 5G frequency

The 4G network tells the device to go to 5G frequency **2175.000 MHz**. No 5G signal can
physically exist there — the frequency does not sit on the standard 5G channel grid. The real 5G
cell is at **632.45 MHz**, an entirely different frequency and radio band.

The device masks this defect by finding the correct cell by itself, so **4G logs the handover as
"Success."** That false success is why this incident reads as a radio problem in standard reporting
when it is not one.

### Nothing stops the loop

The 4G network cannot see the 5G core's rejection, so it reissues the same bad instruction every
time. The device has no valid reason to refuse. **No component in the chain learns from the failure.**

---

## Slide 3 — Business Impact

| Metric | Measured Value |
|---|---|
| **5G registration success rate** | **0% (0 of 20)** |
| Session teardowns | 20 in 18.3 min — **1.1 per minute** |
| Service interruption per teardown | 3.5 – 4.3 s (mean **3.96 s**) |
| **Total time in teardown/recovery** | **79.2 s = 7.2% of the observation window** |
| 4G carrier aggregation lost per event | 3 of 4 carriers dropped and rebuilt, every cycle |
| Signal quality (the presumed culprit) | **Healthy — fully exonerated** |

### What the customer experiences

- **No 5G service at all**, while the handset displays a strong 5G signal — the most damaging
  possible failure mode for brand and NPS.
- **Uplink transfers stall or fail** every ~58 seconds. This log is an uplink throughput test; the
  measured workload is disrupted continuously.
- **4G is degraded too, not just 5G.** Each cycle tears down and rebuilds 3 of 4 aggregated
  carriers, so peak 4G speeds collapse repeatedly even when the device stays on 4G.

### Why this is likely far wider than one device

The rejection is a **core-network capacity condition, not a device fault**. Every device in this
tracking area attempting 5G will hit the same refusal. Worse, because the back-off timer is missing,
**every affected device retries aggressively** — adding signalling load to an already-congested
core. This is a **positive feedback loop that amplifies the congestion it is reporting.**

> **Escalation rationale:** the missing timer converts a routine capacity issue into a
> self-reinforcing signalling storm with the potential to degrade the core for all subscribers.

---

## Slide 4 — Ownership & Required Actions

| # | Action | Owner | Priority | Effect |
|---|---|---|---|---|
| 1 | Include the **`T3346` back-off timer** with every congestion rejection, per 3GPP TS 24.501 | **Core / 5GC (AMF)** | **P1 — immediate** | Collapses 20 teardowns into 1. Stops the retry storm. |
| 2 | Investigate **AMF capacity** for TAC 37302 and clear the congestion condition | **Core / 5GC** | **P1** | Restores 5G service. |
| 3 | Correct the 5G redirect frequency: **435000 → 126490**, and populate the band list | **RAN** | **P2** | Removes reliance on device self-recovery. |
| 4 | Suppress repeated 5G redirect for a device just refused by the 5G core | **RAN** | **P3** | Defence in depth; breaks the loop even if #1 regresses. |
| 5 | Add an alarm on *"congestion rejection issued without back-off timer"* | **Core / OSS** | **P2** | This defect was invisible to existing monitoring. |

### Sequencing

**Action 1 is the highest-leverage fix and is independent of the capacity problem.** It should ship
first: even while congestion persists, a correct back-off timer eliminates the thrash loop, the
carrier-aggregation churn, and the amplifying signalling load. Action 2 restores service. Actions
3–4 are RAN hygiene that remove the hidden dependence on device-side recovery.

### Analytics gap — remediated

The diagnostic tooling **could not have found this cause**. A parser defect silently discarded
100% of 5G core rejection messages, so the evidence pointed at the radio while the true cause sat
in un-indexed core signalling. The parser has been corrected and the finding is now reproducible
from tooling. **Recommend auditing other automated log pipelines for the same class of
silent-drop defect** — a monitoring blind spot of this shape will hide the next incident too.
