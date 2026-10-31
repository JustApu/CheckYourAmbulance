# Mobile inspection experience prototype ([issue #18](https://github.com/JustApu/CheckYourAmbulance-archive/issues/18))

Open [index.html](index.html) in a modern browser, including on a phone. No build tools, packages, backend, authentication, storage, or network are needed. **This is not production code.** All people, vehicles, items, deficiencies, and checks are explicitly synthetic. Reloading or using **Reset Demo** discards the simulated state.

## Owner-requested R1 sealed-kit proposal

Open [sealed-kit.html](sealed-kit.html) from the checklist. It demonstrates a synthetic individually identified kit, seal identity and certifier/time, valid-seal verification, reasoned break, freshly entered quantities/expirations (never prefilled from prior records), a new physical seal marked with the EMT ID and auto-computed earliest expiration, with actor attribution, deficiency persistence, and a manually requested full-content audit. **This is a UX proposal pending SPEC §25.4 amendment approval**, because SPEC §2.1 currently defers detailed seal tracking to R2 and STK-C's random-audit cadence remains undecided. Any authorized user with the applicable role may break/reseal; no second approver is proposed. The prototype uses the browser's local calendar date for expiration validation, a synthetic actor and simulated event sequence, not server time or a genuine tamper-proof ledger.

**Seal verification is distinct from Bulk Pass:** a valid seal provides auditable substitution for only seal-eligible contents; it does not create individual Passed checks. The proposal retains exact quantity/expiration checks on opening, and prevents a seal from substituting when the demo has unresolved count/expiration problems. Sealing never silently resolves deficiencies. Automatic random-audit selection remains out of scope until OQ-14 is decided.

**Bulk Pass** remains in the original screen only to evaluate the existing approved BLK-1–BLK-5; the owner questions its practical value given one-tap individual checks and seal substitution. Deferring/removing it requires an approved SPEC amendment, not an undocumented prototype edit.

## Suggested five-minute walkthrough

1. Start in **Priority** and switch to **Normal**. Toggle **Needs Attention Only** and confirm the suspended item disappears only from the attention-only list (not the full list).
2. Check the known deficiency on the oxygen mask and the separate **Known Deficiency — Check Suspended** item. Neither is a Pass.
3. Tap **Pass** on an unchecked item. Watch **Pending → Saved**; note there is no Save button.
4. Tap **Pass** on *Cleaning wipes*, previously checked by synthetic EMT Morgan. Inspect the duplicate warning and choose **Cancel**, then **Proceed**; both attributed records remain visible.
5. Tap **Unable** on an unchecked item. Submit an empty reason (blocked), then enter a reason. The result remains attention-needed with a synthetic deficiency.
6. Use **Bulk Pass Eligible**. Read the item list and exclusions before confirming. Verify measurement/special checks, known deficiencies and suspended checks cannot be bulk-passed.
7. Toggle **Simulate next save failure**, record a check and observe **Failed / Retry**. Toggle **Simulate connection loss** and observe the banner and disabled confirmation. Restore connection and retry.
8. Follow the **sealed-kit** link: verify matching seal ID and integrity; request a full audit; break with reason; inspect and change quantities; reseal with the EMT ID written on the replacement seal; verify that deficiencies are not erased. Reset to test again.
9. Try at a phone viewport around 320, 375 and 430 CSS pixels; test keyboard Tab/Enter and a screen reader if available. Large touch targets, explicit text statuses and accessible modal labels are intended.

## What this demonstrates — and what it does not

- Simulates UI states, conflict warnings, eligibility, and deficiency presentation without network calls.
- **Does not** implement server-side authorization, immutable history, atomic concurrency, actual idempotency, persisted preference synchronization, accurate schedule calculations, or deficiency workflow transitions. A browser tab cannot emulate two real concurrent sessions. No simulated **Saved** label means a real server acknowledged anything.
- The scenario intentionally uses a previously recorded check to demonstrate a duplicate warning. A production server must detect changes to the viewer's basis at write time (SPEC CON-3), not merely the presence of any prior check. The prototype's warning is illustrative only.
- Only outcomes applicable to the synthetic items are shown. Real checklist content is pending OQ-01.
- **OQ-11**: evaluate deferring/removing generic Bulk Pass, which the owner reports may add more risk than benefit.
- **OQ-14 / R1 release scope**: minimum seal identity, substitution, certification, break/reseal and manual audit needed in R1; automated random audit cadence deferred.
- **OQ-15**: review wording and placement of Pending/Saved/Failed, stale-view banner, duplicate warning and feedback on retries.
- **OQ-40**: review touch targets, typography, glove use, outdoor glare, scrolling and the Priority/Normal controls.

## Collect feedback without personal data

Owner action is required to recruit pilot participants; the implementation agent cannot assert that this occurred. Post anonymized feedback directly on [issue #18](https://github.com/JustApu/CheckYourAmbulance-archive/issues/18) using this template:

```text
[Agent: Human] Prototype field feedback (anonymous)
Reviewer group: EMT / supervisor / other (no names)
Device size/type: e.g. phone ~375 CSS px (no device identifiers)
Tasks attempted: checklist / duplicate / bulk / unable / offline / filter
Worked well:
Confusing or difficult:
Safety concern or missed action:
Suggested change:
Proposed SPEC question: OQ-15 / OQ-40 / neither
```

Do not collect patient, crew, phone/account identifiers or actual ambulance data. A reviewer can note a participant count without identities. Any behavior change is a **proposal** and must follow the SPEC amendment process (§25.4); this prototype cannot silently override the approved SPEC. The feedback requirement is **not satisfied** until actual anonymous feedback is posted.

### Owner usability feedback: prerequisite-gated actions

Seal verification is disabled until the inspector types the matching physical seal ID and attests to integrity. Breaking the seal is disabled until a reason is selected. Resealing is disabled until every observed count and expiration is manually entered, the EMT ID written on the new seal is entered, and the inspector attests to the complete contents check. An adjacent status line explains what remains. Existing certified values are shown in the kit summary but **never copied into inspection inputs**. Counts outside target are recorded as unresolved deficiencies, not treated as incomplete form entry. No backend validation is implied by this static demonstration.

### Current unnumbered-seal workflow (owner clarification)

Physical seals are **not uniquely numbered**. The crew member who seals a kit handwrites their identifier and the earliest applicable expiration date on the seal; many seals may carry the same EMT ID. The prototype accepts repeated crew-member identifiers, automatically calculates the earliest expiration from all observed contents and requires the crew member to attest to writing that date on the physical seal, and uses the kit identity plus attributed event history for traceability. The former unique-seal-ID validation was wrong. A future switch to individually numbered/prefabricated seals should be modeled as an optional seal identifier, not a retroactive requirement.

### Seal expiration scope correction

Owner challenged artificial example of an item physically inside a kit but excluded from seal substitution. Prototype now treats **every listed item physically in the sealed kit** as covered by the valid seal and uses the **earliest expiration across all kit contents** as the handwritten date. Any equipment requiring ongoing independent functional checks should be modeled outside the sealed contents, not as a hidden exception within this demonstration. This is a proposed R1 workflow rule pending formal SPEC approval.

### Supervisor audit and expiration regression (owner tested)

A supervisor-requested audit blocks seal-only verification until the kit is opened, **every item** is inspected, and the kit is resealed. On completion, the audit is marked fulfilled and the new seal can be verified normally. The owner successfully retested the flow in Firefox on macOS and Chrome on Android. The date picker and reseal validation reject expiration dates before today but accept today's date; quantities below the expected count are recorded as deficiencies rather than silently cleared. Test again after any workflow change. This remains a static simulation; no server-side state or security guarantees are provided.
