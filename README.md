# Blind Tender (`BlindTender`)

**Commit-Reveal Sealed-Bid Vickrey Reverse Auction with Price-Blind GenLayer Consensus Eligibility** — applied to **Utility Grid-Scale Battery Energy Storage System (BESS) Procurement**.

`BlindTender` separates **sealed price discovery** from **subjective technical compliance verification**: vendors commit hidden prices under a SHA-256 commitment scheme and lock ceiling-5% native wei bid bonds, GenLayer validators independently fetch commit-pinned engineering dossiers **without ever seeing bid prices** to classify technical eligibility (`ELIGIBLE`, `DISQUALIFIED`, or retriable `UNVERIFIABLE`), and deterministic Python code awards the contract to the lowest-price `ELIGIBLE` bidder at the **second-lowest `ELIGIBLE` clearing price** (Vickrey reverse auction).

---

## Why This Requires an Intelligent Contract

1. **Price-Blind Technical Eligibility**: Traditional smart contracts cannot read unstructured engineering dossiers (e.g., UL-9540A thermal-runaway safety test status, usable MWh capacity, warranted cycle life). `BlindTender` snapshots only the tender's mandatory technical requirements and the vendor's commit-pinned dossier before entering `gl.vm.run_nondet_unsafe`, guaranteeing that bid prices never enter the LLM prompt.
2. **Lowball Disqualification Without Price Bias**: In real utility procurement, the cheapest bidder often cuts corners on mandatory safety certifications. In our live StudioNet run, Vendor A bid `417` wei (cheapest) but lacked UL-9540A certification (`DISQUALIFIED:SAFETY_CERT_MISSING`), so deterministic Vickrey clearing ignored the lowball bid and awarded Vendor B (`673` wei) at Vendor C's second-lowest eligible price (`829` wei).
3. **Truthful Bidding & Anti-Ghosting**: Vickrey second-price clearing makes truthful cost bidding a weakly dominant strategy among eligible vendors, while SHA-256 commit-reveal prevents last-minute undercutting and unrevealed ghost bidders forfeit their 5% bid bonds to the Buyer.

---

## Architecture & Security Invariants

| Property | Enforcement Mechanism |
|---|---|
| **Zero Stranded Native Escrow** | Separate `@gl.public.write.payable def deposit_escrow()` credits `pending_withdrawals[caller]`. Non-payable `open_tender()` and `commit_bid()` debit from internal escrow, preventing GenVM `UserError` reverts from stranding `msg.value`. |
| **Native Pull Withdrawals** | `withdraw_payout()` follows Checks-Effects-Interactions (CEI) and transfers real native wei via `_EoaRecipient(sender_addr).emit_transfer(value=u256(amount))`. |
| **Sealed-Bid Integrity** | `commit_bid` locks `sha256(f"{tender_id}\|{bid_id}\|{bidder}\|{price}\|{salt}")` and exact ceiling-5% bid bond (`max(10, (reserve_price * 5 + 99) // 100)`). `reveal_bid` recomputes the exact digest (`COMMITMENT_MISMATCH` blocks tampering). |
| **Commit-Pinned Evidence** | Every tender freezes `canonical_repo_prefix` (`https://raw.githubusercontent.com/<owner>/<repo>/`). `reveal_bid` rejects mutable `/main/` URLs (`INVALID_EVIDENCE_URL`), foreign repos (`REPO_PREFIX_MISMATCH`), and verifies SHA-256 byte digests inside `leader_fn` / `validator_fn`. |
| **Disjoint Verdict/Reason Lattice** | `BID_COMPATIBLE_REASONS` maps `ELIGIBLE -> {ALL_REQUIREMENTS_MET}`, `DISQUALIFIED -> {SAFETY_CERT_MISSING, CAPACITY_BELOW_MIN, CYCLE_LIFE_DEFICIENT}`, `UNVERIFIABLE -> {FETCH_OR_PARSE_FAILURE, HASH_MISMATCH, AMBIGUOUS_DOSSIER}`. Checked in `normalize_bid_eval`, `validate_leader_bid_eval`, and pre-storage assertion. |
| **Retriable `UNVERIFIABLE` & Safe Unwind** | Conclusive `ELIGIBLE`/`DISQUALIFIED` verdicts are immutable (`BID_ALREADY_CONCLUDED`). Transient `UNVERIFIABLE` verdicts remain retriable up to `MAX_EVAL_ATTEMPTS = 5`, after which `unwind_unverifiable_tender` refunds 100% of reserves and bid bonds. |
| **Global Conservation Invariant** | `total_locked_reserves + total_locked_bonds + total_pending_withdrawals + total_withdrawn == total_deposited` and `contract_balance == total_locked_reserves + total_locked_bonds + total_pending_withdrawals`. |

---

## Live StudioNet Deployment & Verification

- **Network**: GenLayer StudioNet (`https://studio.genlayer.com/api`, Chain ID `61999`)
- **Runner**: `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
- **Deployed Contract**: [`0xfE39e345c7497B913E6143f629dDfFDc2E26be75`](https://explorer-studio.genlayer.com/address/0xfE39e345c7497B913E6143f629dDfFDc2E26be75)
- **Deploy Transaction**: [`0x39281b5eda7223f3b27a7e39ec25e6c633ecc268367715733fc3785c3a307975`](https://explorer-studio.genlayer.com/tx/0x39281b5eda7223f3b27a7e39ec25e6c633ecc268367715733fc3785c3a307975) (`FINALIZED`, `MAJORITY_AGREE`)
- **Pinned Source Commit**: `2fbb47405ace9afc0eda74f86c53a0fa48b3f207` (`deployed_source_byte_identical: true`, SHA-256 `e7df82e6a2fff3fd70e942ff249d30d4de4c02a61494dabcc78c008892c06e13`)
- **Full Machine-Readable Proof**: [`release_manifest.json`](./release_manifest.json)

### Live End-to-End Vickrey Auction (`bess-live-01`)

| Step | Actor | Action / Outcome | StudioNet Tx |
|---|---|---|---|
| `S0`–`S0C` | Buyer & 3 Vendors | Native escrow deposits: Buyer `1000` wei; Vendors A, B, C `50` wei each | [`0xaa497b...`](https://explorer-studio.genlayer.com/tx/0xaa497b84d6e4ce73c5751a2524e53dee50ea7cc664b7c5c44950febade109efd) |
| `S1` | Buyer | `open_tender("bess-live-01", reserve=1000, max_bidders=3)` -> `MIN_BOND_50` | [`0xd0d31f...`](https://explorer-studio.genlayer.com/tx/0xd0d31fcf8ae323c305efde30c21647ef2d437cab57bbdb98ad17927ebc8bd857) |
| `S2A`–`S2C` | Vendors A, B, C | Sealed SHA-256 bid commits (`417`, `673`, `829` hidden); 3rd commit opens `REVEAL_OPEN` | [`0x959441...`](https://explorer-studio.genlayer.com/tx/0x959441ac097be0f5b3160c01f10dbe7e1971c9f3fd84352ed78c6b72af333ad7) |
| `S3A`–`S3C` | Vendors A, B, C | Verified reveals with commit-pinned engineering dossiers (`bid-a:417`, `bid-b:673`, `bid-c:829`) | [`0x8ee441...`](https://explorer-studio.genlayer.com/tx/0x8ee44100dd6498cbe2b48e7e913ae507f9228c6a21b865eec77f0de78572a9c1) |
| `S4A` | Auditor | Price-blind `evaluate_bid("bid-a")` -> **`DISQUALIFIED:SAFETY_CERT_MISSING`** | [`0xb12678...`](https://explorer-studio.genlayer.com/tx/0xb12678ac62e85a97c69cc67716f017d83eea588d0279a87bcd7c2146448486a0) |
| `S4B` | Auditor | Price-blind `evaluate_bid("bid-b")` -> **`ELIGIBLE:ALL_REQUIREMENTS_MET`** | [`0xb0ac2f...`](https://explorer-studio.genlayer.com/tx/0xb0ac2fc1243f83d048b38936ec995f7fbe2ed9b41f1c962e858967d8b23f6f49) |
| `S4C` | Auditor | Price-blind `evaluate_bid("bid-c")` -> **`ELIGIBLE:ALL_REQUIREMENTS_MET`** | [`0xf18225...`](https://explorer-studio.genlayer.com/tx/0xf18225658387e2f662fa478885fb4575397b24e120f7ae447b15f419c798f0e9) |
| `S5` | Auditor | `award_tender("bess-live-01")` -> **`AWARDED:bess-live-01:bid-b:PRICE_829`** | [`0x9b5cb5...`](https://explorer-studio.genlayer.com/tx/0x9b5cb5a63915374d6d75e4e26ea5f0bd4731ace40ef5552b4fdacf5617d45910) |
| `S6A`–`S6D` | All 4 Actors | Native `withdraw_payout()`: Vendor B `879`, Buyer `171`, Vendor A `50`, Vendor C `50` -> `contract_balance = 0` | [`0x54718a...`](https://explorer-studio.genlayer.com/tx/0x54718ab086b13f42038563034e3eb77b681775b5f470f2b6ba8bd59f29c415f9) |

### Live Expected-Failure Abuse Guards (`X1`–`X9`, All `FINALIZED` / `MAJORITY_AGREE`)

| Guard | Expected `UserError` | StudioNet Tx |
|---|---|---|
| `X1` | `INSUFFICIENT_ESCROW_BALANCE` (zero stranded balance) | [`0x5cc248...`](https://explorer-studio.genlayer.com/tx/0x5cc248d69a8ea7bf4122820284cb4419ecb7fd2ba4d74fa35367bd135ad6b192) |
| `X2` | `ZERO_DEPOSIT` | [`0x36dc96...`](https://explorer-studio.genlayer.com/tx/0x36dc965851cad8ce4ad0ba9a228c5af067125eab3210a50bfd5b83c489814c25) |
| `X3` | `INVALID_REPO_PREFIX` | [`0x0d0a6f...`](https://explorer-studio.genlayer.com/tx/0x0d0a6f7687672547a1c5262f3bf444eb18fe95f5280d1107240ab74244a45f08) |
| `X4` | `INSUFFICIENT_BID_BOND` (`49 < 50`) | [`0x47058d...`](https://explorer-studio.genlayer.com/tx/0x47058da12e53adb2fe2e2721305ab0e5a0231218793a3afa7a83f8a0d7706b62) |
| `X5` | `BUYER_CANNOT_BID` | [`0x4c5ff0...`](https://explorer-studio.genlayer.com/tx/0x4c5ff0ec04109d0707eb9e2b040b1ff1e2c02e3ad5958e5fb195432b3631d6df) |
| `X6` | `COMMITMENT_MISMATCH` (tampered reveal price) | [`0x063e23...`](https://explorer-studio.genlayer.com/tx/0x063e23e81902b437d70af78d3adea248fb5d42afcc86028e38382705885c056d) |
| `X7` | `INVALID_EVIDENCE_URL` (mutable `/main/` URL) | [`0x04e070...`](https://explorer-studio.genlayer.com/tx/0x04e070447024a3d8b1460663ccfc92ae603573d8556b2fc0b75aa62e90b97ad7) |
| `X8` | `BID_ALREADY_CONCLUDED` (replay evaluation) | [`0x804b8c...`](https://explorer-studio.genlayer.com/tx/0x804b8ca1cb1abaeb0c5b8e183a3aae680cff5679ce4076f3058a01c791f6c8a1) |
| `X9` | `NO_PENDING_PAYOUT` (unauthorized withdrawal) | [`0x434989...`](https://explorer-studio.genlayer.com/tx/0x434989a436ab7f4da9f7362ad5b61f8c96c86093d5a2414a58e74c396aa00768) |

---

## Local Verification

```bash
# Run all 10 unit & invariant tests (price-blind prompt proof, Vickrey clearing,
# ghost-bond slashing, Byzantine leader rejection, UNVERIFIABLE unwind, conservation)
pytest tests -v

# Re-run live StudioNet runner probe & end-to-end demonstration
cd scripts && npm install
node probe_runner.js
node live_demo.js
```
