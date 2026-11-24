# Blind Tender (`BlindTender`)

**Commit-Reveal Sealed-Bid Vickrey Reverse Auction with Price-Blind GenLayer Consensus Eligibility** — a domain-reusable procurement Intelligent Contract demonstrated on **Utility Grid-Scale Battery Energy Storage System (BESS) Procurement**.

`BlindTender` separates **sealed price discovery** from **subjective technical compliance verification**: vendors lock ceiling-5% native wei bid bonds and commit `sha256(tender_id|bid_id|bidder|price|content_hash|salt)` binding **both** their hidden price and their technical dossier digest before any rival reveals. GenLayer validators independently fetch commit-pinned unstructured technical prose dossiers **without ever seeing bid prices or unverified bidder summaries** to classify compliance (`ELIGIBLE`, `DISQUALIFIED`, or retriable `UNVERIFIABLE`), and deterministic Python code awards the lowest-price `ELIGIBLE` bidder at the **second-lowest `ELIGIBLE` clearing price** (Vickrey reverse auction).

---

## Why This Requires an Intelligent Contract

1. **Unstructured Prose Compliance Without Price Bias**: Real procurement dossiers are heterogeneous engineering submittals, lab attestations, and qualification memos — not uniform JSON flags. `BlindTender` snapshots only the tender's `<MANDATORY_REQUIREMENTS>` and the SHA-256-verified technical dossier bytes before entering `gl.vm.run_nondet_unsafe`, guaranteeing neither bid prices nor unverified bidder summaries ever enter the LLM prompt.
2. **Lowball Disqualification**: In our live StudioNet run, Vendor A bid `417` wei (cheapest) but its engineering submittal prose noted that unit-level UL-9540A thermal-runaway burn testing was deferred to Q4 (`DISQUALIFIED:COMPLIANCE_CERT_MISSING`). Deterministic Vickrey clearing ignored the disqualified lowball bid and awarded Vendor B (`673` wei) at Vendor C's second-lowest eligible price (`829` wei).
3. **Anti-Tampering & Anti-Griefing Economics**: Binding `content_hash` inside `commit_hash` prevents post-reveal dossier swapping to back out of a winning price; `HASH_MISMATCH`, unrevealed ghost bids, and single-bidder exhausted `UNVERIFIABLE` links forfeit their 5% bid bonds to the Buyer without blocking honest bidders from settling.

---

## Architecture & Security Invariants

| Property | Enforcement Mechanism |
|---|---|
| **Zero Stranded Native Escrow** | Separate `@gl.public.write.payable def deposit_escrow()` credits `pending_withdrawals[caller]`. Non-payable `open_tender()` and `commit_bid()` debit internal escrow so GenVM `UserError` reverts never strand `msg.value`. |
| **Native Pull Withdrawals** | `withdraw_payout()` follows CEI and transfers real native wei via `_EoaRecipient(sender_addr).emit_transfer(value=u256(amount))`. |
| **Dual-Bound Sealed Commit** | `commit_bid` locks `sha256(f"{tender_id}\|{bid_id}\|{bidder}\|{price}\|{content_hash}\|{salt}")` and exact ceiling-5% bid bond (`max(10, (reserve_price * 5 + 99) // 100)`). `reveal_bid` blocks both price tampering and post-reveal dossier swapping (`COMMITMENT_MISMATCH`). |
| **Commit-Pinned Unstructured Prose** | Every tender freezes `canonical_repo_prefix`. `reveal_bid` rejects `/main/` URLs (`INVALID_EVIDENCE_URL`) and off-repo URLs (`REPO_PREFIX_MISMATCH`); `evaluate_bid` verifies UTF-8, `<= 2500` B cap, and exact SHA-256 match. |
| **Domain-Reusable Disjoint Lattice** | `BID_COMPATIBLE_REASONS`: `ELIGIBLE -> {ALL_REQUIREMENTS_MET}`, `DISQUALIFIED -> {TECHNICAL_SPEC_UNMET, COMPLIANCE_CERT_MISSING, HASH_MISMATCH}`, `UNVERIFIABLE -> {FETCH_OR_PARSE_FAILURE, AMBIGUOUS_DOSSIER}`. Exact canonical JSON equality enforced between leader and validators. |
| **Anti-Griefing & Systemic Unwind** | `HASH_MISMATCH` concludes `DISQUALIFIED` immediately and forfeits the bidder's bond. A single bidder exhausting `MAX_EVAL_ATTEMPTS = 5` on a broken URL is marked `FORFEITED_UNVERIFIABLE` (bond forfeited) while `award_tender` proceeds for honest bidders. `unwind_unverifiable_tender` unlocks only if **all** revealed bids exhaust retries (systemic outage) or `unwind_deadline` passes. |
| **Global Conservation Invariant** | `total_locked_reserves + total_locked_bonds + total_pending_withdrawals + total_withdrawn == total_deposited` and `contract_balance == total_locked_reserves + total_locked_bonds + total_pending_withdrawals`. |

---

## Live StudioNet Deployment & Verification

- **Network**: GenLayer StudioNet (`https://studio.genlayer.com/api`, Chain ID `61999`, `genlayer-js@2.0.0`)
- **Runner**: `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` (probe [`0xA42C3c...`](https://explorer-studio.genlayer.com/address/0xA42C3c77331FCBa0D13a2e31FAe099deB29ADB31))
- **Deployed Contract**: [`0x33e1CA90d4ff4E58ECed6De49d40A4ee155356c4`](https://explorer-studio.genlayer.com/address/0x33e1CA90d4ff4E58ECed6De49d40A4ee155356c4)
- **Deploy Transaction**: [`0x10736e2eee43d6a72c3bdc7b5592900ea39461b7394813cd3627c7d90ad571cd`](https://explorer-studio.genlayer.com/tx/0x10736e2eee43d6a72c3bdc7b5592900ea39461b7394813cd3627c7d90ad571cd) (`FINALIZED`, `MAJORITY_AGREE`)
- **Pinned Source Commit**: [`780698d213d0673c148210a0a3766574bf3e6eb2`](https://github.com/Kanezar3/Blind-Tender/blob/780698d213d0673c148210a0a3766574bf3e6eb2/contracts/blind_tender.py) (`deployed_source_byte_identical: true`, SHA-256 `ce9f9d9a4341bc23feee367eb0e09345ab5519ae450b510d08a856ac098830c5`)
- **Full Machine-Readable Proof**: [`release_manifest.json`](./release_manifest.json)

### Live End-to-End Vickrey Auction (`bess-live-v2`)

| Step | Actor | Action / Outcome | StudioNet Tx |
|---|---|---|---|
| `S0`–`S0C` | Buyer & 3 Vendors | Native escrow deposits: Buyer `1000` wei; Vendors A, B, C `50` wei each | [`0x1aaa75...`](https://explorer-studio.genlayer.com/tx/0x1aaa75873f1b79b35ed5d60b325aae9e88e3a90b694c9536a74abb589568f15c) |
| `S1` | Buyer | `open_tender("bess-live-v2", reserve=1000, max_bidders=3)` -> `MIN_BOND_50` | [`0x726e77...`](https://explorer-studio.genlayer.com/tx/0x726e7759225b46cba7d1d2ba3d4c7af915a32bf98838bef16d3bde522dcdb727) |
| `S2A`–`S2C` | Vendors A, B, C | Sealed SHA-256 `(price + dossier_hash + salt)` commits; 3rd commit opens `REVEAL_OPEN` | [`0x2b1db4...`](https://explorer-studio.genlayer.com/tx/0x2b1db4370bd9a3294306f0a2c5f5c4d91953acfd45516c90378ab3ce95efd258) |
| `S3A`–`S3C` | Vendors A, B, C | Verified reveals with commit-pinned unstructured prose dossiers (`417`, `673`, `829`) | [`0xa011ec...`](https://explorer-studio.genlayer.com/tx/0xa011ec05da7e3c230d42b32d266f82542d6d4f44b22e933795f8ee90086e5b34) |
| `S4A` | Auditor | Price-blind `evaluate_bid("bid-a")` -> **`DISQUALIFIED:COMPLIANCE_CERT_MISSING`** | [`0xbf0972...`](https://explorer-studio.genlayer.com/tx/0xbf097299382277a736849f2ad38e710a60ea558c84de64332d3b7b27af4b0b57) |
| `S4B` | Auditor | Price-blind `evaluate_bid("bid-b")` -> **`ELIGIBLE:ALL_REQUIREMENTS_MET`** | [`0x66a0a3...`](https://explorer-studio.genlayer.com/tx/0x66a0a3cb914cd173a82084beafb0bed1108f4eacc7ba260f7d8fc8e85724c9e6) |
| `S4C` | Auditor | Price-blind `evaluate_bid("bid-c")` -> **`ELIGIBLE:ALL_REQUIREMENTS_MET`** | [`0xe8d443...`](https://explorer-studio.genlayer.com/tx/0xe8d4433b5c1c4a561392a523bd615accaa49d83fd936212bebc90585f1a19ae7) |
| `S5` | Auditor | `award_tender("bess-live-v2")` -> **`AWARDED:bess-live-v2:bid-b:PRICE_829`** | [`0x457faa...`](https://explorer-studio.genlayer.com/tx/0x457faa4d254fcb801d285870667e8825633943fd02b10f037d900a66e68f8496) |
| `S6A`–`S6D` | All 4 Actors | Native `withdraw_payout()`: Vendor B `879`, Buyer `171`, Vendor A `50`, Vendor C `50` -> `contract_balance = 0` | [`0x6b6be9...`](https://explorer-studio.genlayer.com/tx/0x6b6be911f9752fffd8032bb75da4e1fbc8491ab190f8cfe469e69dce453325d5) |

### Live Expected-Failure Abuse Guards (`X1`–`X11`, All `FINALIZED` / `MAJORITY_AGREE`)

| Guard | Expected `UserError` | StudioNet Tx |
|---|---|---|
| `X1` | `INSUFFICIENT_ESCROW_BALANCE` (zero stranded balance) | [`0xccde67...`](https://explorer-studio.genlayer.com/tx/0xccde67f06140a0e712c31082da3a224c1d3dae400930eb25d51eb3b1e5ba7a7e) |
| `X2` | `INSUFFICIENT_BID_BOND` (`49 < 50`) | [`0x876a24...`](https://explorer-studio.genlayer.com/tx/0x876a246fd4ea84ec41da8c4d05860903064eb67e2429f6559697935743fed42d) |
| `X3` | `BUYER_CANNOT_BID` | [`0x2eb0b1...`](https://explorer-studio.genlayer.com/tx/0x2eb0b1eecb36edb86fb1732f404cef7468b307292aba903e87407a57c479ad2e) |
| `X4` | `UNAUTHORIZED_CALLER` (spoofed reveal) | [`0x19d205...`](https://explorer-studio.genlayer.com/tx/0x19d205e3f3195d7e5fbc4fe11e4a0ae5c1e0ae6a4ec54c54f0c1b8e24fec6bfd) |
| `X5` | `COMMITMENT_MISMATCH` (tampered reveal price) | [`0x954f71...`](https://explorer-studio.genlayer.com/tx/0x954f7114fd0c3cfa459414121cb5454573337e3a3bb822d1b6b47496cadf99cc) |
| `X6` | `COMMITMENT_MISMATCH` (post-reveal dossier swap) | [`0xd4aee9...`](https://explorer-studio.genlayer.com/tx/0xd4aee976d79a1f3474b928bb3f24a8a3a08745d17cec246f14a7d7e263a22e62) |
| `X7` | `INVALID_EVIDENCE_URL` (mutable `/main/` URL) | [`0xb08f6b...`](https://explorer-studio.genlayer.com/tx/0xb08f6bb3537f4e90c749bf454ad68179f240d6da9b187389ee67227287a68706) |
| `X8` | `REPO_PREFIX_MISMATCH` (foreign repo URL) | [`0x930155...`](https://explorer-studio.genlayer.com/tx/0x9301559184af2346c391545ca5729ce0aaee6bbaacd3df63dddf44309f7eba1c) |
| `X9` | `UNEVALUATED_BID_EXISTS` (premature `award_tender`) | [`0x86d38f...`](https://explorer-studio.genlayer.com/tx/0x86d38f4cfff981bee04f5d681f97aa62f04c5a7156625c342aefe0e39f2bca30) |
| `X10` | `BID_ALREADY_CONCLUDED` (replay evaluation) | [`0xb7e9b5...`](https://explorer-studio.genlayer.com/tx/0xb7e9b5d827a516135ab1c30426c6daece7eb89fcdccbe8e84e8803dcdf43a71f) |
| `X11` | `NO_PENDING_PAYOUT` (unauthorized withdrawal) | [`0x9f60ca...`](https://explorer-studio.genlayer.com/tx/0x9f60caa27a191ae92927f2baac9e01988b1c88d38fbd2cfca9c32570e026cf6a) |

---

## Scope & What the Live Demo Does Not Prove

- StudioNet accounts are funded via testnet `sim_fundAccount` wei rather than mainnet capital.
- Technical prose dossiers are commit-pinned inside one canonical demo repository (`Kanezar3/Blind-Tender`) to prevent arbitrary off-repo fetches during automated verification.
- Ghost-bidder forfeiture, `HASH_MISMATCH` bond slashing, single-bidder 404 anti-griefing, and systemic-outage `unwind_unverifiable_tender` are verified in local `pytest` unit tests rather than live deadline-expiry runs.

---

## Local Verification

```bash
# Run all 10 unit & invariant tests
pytest tests -v

# Re-run live StudioNet runner probe & end-to-end demonstration
cd scripts && npm install
node probe_runner.js
node live_demo.js
```
