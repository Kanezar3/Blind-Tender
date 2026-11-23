import hashlib
import json
from pathlib import Path

import pytest
from conftest import (
    ADDR_ATTACKER,
    ADDR_AUDITOR,
    ADDR_BUYER,
    ADDR_VENDOR_A,
    ADDR_VENDOR_B,
    ADDR_VENDOR_C,
    MockWebResponse,
    UserError,
    emitted_transfers,
    exec_prompt_call_history,
    gl_mock,
    mock_llm_dict,
    mock_web_responses,
    set_sender,
    set_time,
    set_value,
)
from contracts.blind_tender import (
    ADJUDICATION_GRACE_SECONDS,
    BID_COMPATIBLE_REASONS,
    BlindTender,
    MAX_EVIDENCE_BYTES,
    MAX_EVAL_ATTEMPTS,
    compute_bid_commitment,
    compute_min_bid_bond,
    normalize_bid_eval,
    select_vickrey_winner,
    validate_leader_bid_eval,
)

REPO_PREFIX = "https://raw.githubusercontent.com/Kanezar3/Blind-Tender/"
COMMIT_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
BASE_URL = f"{REPO_PREFIX}{COMMIT_SHA}/evidence/"

MANDATORY_REQS = (
    "Utility Grid-Scale BESS Procurement: usable capacity >= 100 MWh, "
    "active UL-9540A thermal-runaway safety certification, and >= 6000 warranted full cycles."
)

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"


def _load_fixture(filename: str) -> tuple[str, bytes, str]:
    raw = (EVIDENCE_DIR / filename).read_bytes()
    assert 0 < len(raw) <= MAX_EVIDENCE_BYTES
    # Verify natural prose fixtures contain no internal verdict/reason tokens
    text = raw.decode("utf-8")
    for token in (
        "ELIGIBLE",
        "DISQUALIFIED",
        "UNVERIFIABLE",
        "SAFETY_CERT_MISSING",
        "ALL_REQUIREMENTS_MET",
        "CAPACITY_BELOW_MIN",
        "CYCLE_LIFE_DEFICIENT",
    ):
        assert token not in text, f"Fixture {filename} leaked internal token {token}"
    url = f"{BASE_URL}{filename}"
    digest = hashlib.sha256(raw).hexdigest()
    return url, raw, digest


def _deposit(contract: BlindTender, sender, amount: int) -> int:
    set_sender(sender)
    set_value(amount)
    bal = contract.deposit_escrow()
    set_value(0)
    return bal


def _assert_conservation(contract: BlindTender) -> dict:
    acc = json.loads(contract.get_accounting_state())
    locked = acc["total_locked_reserves"] + acc["total_locked_bonds"]
    assert locked + acc["total_pending_withdrawals"] + acc["total_withdrawn"] == acc["total_deposited"]
    assert acc["contract_balance"] == locked + acc["total_pending_withdrawals"]
    return acc


def _register_default_fixtures() -> dict[str, tuple[str, str]]:
    out = {}
    for key, fname in (
        ("a", "bid_a_lowball_no_cert.json"),
        ("b", "bid_b_compliant_winner.json"),
        ("c", "bid_c_compliant_runnerup.json"),
    ):
        url, raw, digest = _load_fixture(fname)
        mock_web_responses[url] = MockWebResponse(status=200, body=raw)
        out[key] = (url, digest)

    mock_llm_dict["LowCost Cells Ltd."] = json.dumps(
        {"verdict": "DISQUALIFIED", "reason_code": "SAFETY_CERT_MISSING"}
    )
    mock_llm_dict["GridVault Energy Corp."] = json.dumps(
        {"verdict": "ELIGIBLE", "reason_code": "ALL_REQUIREMENTS_MET"}
    )
    mock_llm_dict["ApexStorage Systems AG"] = json.dumps(
        {"verdict": "ELIGIBLE", "reason_code": "ALL_REQUIREMENTS_MET"}
    )
    return out


# -----------------------------------------------------------------------------
# 1. Happy-Path Vickrey Reverse Auction + Price-Blind Isolation Proof
# -----------------------------------------------------------------------------
def test_happy_path_vickrey_award_disqualifies_cheapest_lowballer():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    # Buyer funds escrow and opens tender (reserve=1000, min_bond=50, max_bidders=3)
    _deposit(c, ADDR_BUYER, 1000)
    res = c.open_tender("bess-2026-01", 1000, 3, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    assert res == "TENDER_OPENED:bess-2026-01:MIN_BOND_50"

    # 3 Vendors deposit 50 wei bonds & commit sealed hashes
    _deposit(c, ADDR_VENDOR_A, 50)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)

    h_a = compute_bid_commitment("bess-2026-01", "bid-a", ADDR_VENDOR_A, 417, "salt-a")
    h_b = compute_bid_commitment("bess-2026-01", "bid-b", ADDR_VENDOR_B, 673, "salt-b")
    h_c = compute_bid_commitment("bess-2026-01", "bid-c", ADDR_VENDOR_C, 829, "salt-c")

    set_sender(ADDR_VENDOR_A)
    assert c.commit_bid("bess-2026-01", "bid-a", 50, h_a) == "BID_COMMITTED:bess-2026-01:bid-a:COMMIT_OPEN"
    set_sender(ADDR_VENDOR_B)
    assert c.commit_bid("bess-2026-01", "bid-b", 50, h_b) == "BID_COMMITTED:bess-2026-01:bid-b:COMMIT_OPEN"
    set_sender(ADDR_VENDOR_C)
    # 3rd bidder reaches max_bidders -> auto-opens REVEAL_OPEN
    assert c.commit_bid("bess-2026-01", "bid-c", 50, h_c) == "BID_COMMITTED:bess-2026-01:bid-c:REVEAL_OPEN"

    # All 3 vendors reveal within reveal window
    set_time(1795000100)
    set_sender(ADDR_VENDOR_A)
    assert (
        c.reveal_bid(
            "bess-2026-01", "bid-a", 417, "salt-a",
            "105 MWh LFP container pack, UL-9540A pending",
            fixtures["a"][0], fixtures["a"][1],
        )
        == "BID_REVEALED:bess-2026-01:bid-a:PRICE_417"
    )
    set_sender(ADDR_VENDOR_B)
    assert (
        c.reveal_bid(
            "bess-2026-01", "bid-b", 673, "salt-b",
            "120 MWh LFP pack, certified UL-9540A, 7200 cycles",
            fixtures["b"][0], fixtures["b"][1],
        )
        == "BID_REVEALED:bess-2026-01:bid-b:PRICE_673"
    )
    set_sender(ADDR_VENDOR_C)
    assert (
        c.reveal_bid(
            "bess-2026-01", "bid-c", 829, "salt-c",
            "115 MWh LFP pack, certified UL-9540A, 6800 cycles",
            fixtures["c"][0], fixtures["c"][1],
        )
        == "BID_REVEALED:bess-2026-01:bid-c:PRICE_829"
    )

    # Permissionless auditor evaluates all 3 bids price-blindly
    set_sender(ADDR_AUDITOR)
    assert c.evaluate_bid("bess-2026-01", "bid-a") == "EVALUATED:bess-2026-01:bid-a:DISQUALIFIED:SAFETY_CERT_MISSING"
    assert c.evaluate_bid("bess-2026-01", "bid-b") == "EVALUATED:bess-2026-01:bid-b:ELIGIBLE:ALL_REQUIREMENTS_MET"
    assert c.evaluate_bid("bess-2026-01", "bid-c") == "EVALUATED:bess-2026-01:bid-c:ELIGIBLE:ALL_REQUIREMENTS_MET"

    # Price-blind isolation proof: none of the bid prices ever appeared in any LLM prompt
    assert len(exec_prompt_call_history) >= 6  # leader + validator for 3 bids
    for prompt in exec_prompt_call_history:
        assert "417" not in prompt
        assert "673" not in prompt
        assert "829" not in prompt

    # Deterministic Vickrey award: VendorB (673) wins over VendorC (829) and clears at 829
    # VendorA (417) was disqualified so its lowball price is ignored
    assert c.award_tender("bess-2026-01") == "AWARDED:bess-2026-01:bid-b:PRICE_829"

    tender = json.loads(c.get_tender("bess-2026-01"))
    assert tender["status"] == "AWARDED"
    assert tender["winner_bid_id"] == "bid-b"
    assert tender["winner_bidder"] == str(ADDR_VENDOR_B)
    assert tender["clearing_price"] == 829

    assert c.get_pending_payout(ADDR_VENDOR_B) == 879  # 829 clearing + 50 bond
    assert c.get_pending_payout(ADDR_BUYER) == 171     # 1000 reserve - 829 clearing
    assert c.get_pending_payout(ADDR_VENDOR_A) == 50   # bond refund
    assert c.get_pending_payout(ADDR_VENDOR_C) == 50   # bond refund

    # All 4 actors withdraw native wei -> contract_balance returns to 0
    for addr, expected in (
        (ADDR_VENDOR_B, 879),
        (ADDR_BUYER, 171),
        (ADDR_VENDOR_A, 50),
        (ADDR_VENDOR_C, 50),
    ):
        set_sender(addr)
        assert c.withdraw_payout() == expected
        assert c.get_pending_payout(addr) == 0

    acc = _assert_conservation(c)
    assert acc["contract_balance"] == 0
    assert acc["total_deposited"] == 1150
    assert acc["total_withdrawn"] == 1150
    assert len(emitted_transfers) == 4


# -----------------------------------------------------------------------------
# 2. Sole Eligible Bidder Clears at Own Price & All-Disqualified -> CLOSED_NO_WINNER
# -----------------------------------------------------------------------------
def test_sole_eligible_bidder_clears_at_own_price_and_no_winner_refunds_buyer():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    # Case A: Sole eligible bidder (1 eligible, 1 disqualified) clears at own price (650)
    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-sole", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_A, 50)
    _deposit(c, ADDR_VENDOR_B, 50)

    set_sender(ADDR_VENDOR_A)
    c.commit_bid("t-sole", "b-a", 50, compute_bid_commitment("t-sole", "b-a", ADDR_VENDOR_A, 400, "sa"))
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-sole", "b-b", 50, compute_bid_commitment("t-sole", "b-b", ADDR_VENDOR_B, 650, "sb"))

    set_sender(ADDR_VENDOR_A)
    c.reveal_bid("t-sole", "b-a", 400, "sa", "LowCost pack", fixtures["a"][0], fixtures["a"][1])
    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("t-sole", "b-b", 650, "sb", "GridVault pack", fixtures["b"][0], fixtures["b"][1])

    set_sender(ADDR_AUDITOR)
    c.evaluate_bid("t-sole", "b-a")
    c.evaluate_bid("t-sole", "b-b")
    assert c.award_tender("t-sole") == "AWARDED:t-sole:b-b:PRICE_650"
    assert c.get_pending_payout(ADDR_VENDOR_B) == 700
    assert c.get_pending_payout(ADDR_BUYER) == 350
    assert c.get_pending_payout(ADDR_VENDOR_A) == 50

    # Case B: All bidders disqualified -> CLOSED_NO_WINNER, full reserve back to Buyer
    _deposit(c, ADDR_BUYER, 500)
    c.open_tender("t-none", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_A, 25)
    _deposit(c, ADDR_VENDOR_C, 25)

    set_sender(ADDR_VENDOR_A)
    c.commit_bid("t-none", "n-a", 25, compute_bid_commitment("t-none", "n-a", ADDR_VENDOR_A, 300, "s1"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-none", "n-c", 25, compute_bid_commitment("t-none", "n-c", ADDR_VENDOR_C, 400, "s2"))

    set_sender(ADDR_VENDOR_A)
    c.reveal_bid("t-none", "n-a", 300, "s1", "LowCost pack 1", fixtures["a"][0], fixtures["a"][1])
    set_sender(ADDR_VENDOR_C)
    c.reveal_bid("t-none", "n-c", 400, "s2", "LowCost pack 2", fixtures["a"][0], fixtures["a"][1])

    set_sender(ADDR_AUDITOR)
    c.evaluate_bid("t-none", "n-a")
    c.evaluate_bid("t-none", "n-c")
    assert c.award_tender("t-none") == "CLOSED_NO_WINNER:t-none"
    assert json.loads(c.get_tender("t-none"))["status"] == "CLOSED_NO_WINNER"
    assert c.get_pending_payout(ADDR_BUYER) == 350 + 500
    assert c.get_pending_payout(ADDR_VENDOR_A) == 50 + 25
    assert c.get_pending_payout(ADDR_VENDOR_C) == 25
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 3. Unrevealed Ghost Bidder Forfeits Bond to Buyer
# -----------------------------------------------------------------------------
def test_unrevealed_ghost_bidder_forfeits_bond_to_buyer():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-ghost", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)

    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-ghost", "b-b", 50, compute_bid_commitment("t-ghost", "b-b", ADDR_VENDOR_B, 700, "sb"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-ghost", "b-c", 50, compute_bid_commitment("t-ghost", "b-c", ADDR_VENDOR_C, 600, "sc"))

    # Only VendorB reveals; VendorC ghosts
    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("t-ghost", "b-b", 700, "sb", "GridVault compliant", fixtures["b"][0], fixtures["b"][1])
    set_sender(ADDR_AUDITOR)
    c.evaluate_bid("t-ghost", "b-b")

    # Premature award before reveal_deadline fails
    with pytest.raises(UserError, match="UNREVEALED_BIDS_PENDING"):
        c.award_tender("t-ghost")

    # After reveal_deadline expires, award succeeds and slashes ghost bond to Buyer
    set_time(1795000601)
    assert c.award_tender("t-ghost") == "AWARDED:t-ghost:b-b:PRICE_700"
    ghost_bid = json.loads(c.get_bid("t-ghost", "b-c"))
    assert ghost_bid["status"] == "FORFEITED_UNREVEALED"
    assert c.get_pending_payout(ADDR_VENDOR_C) == 0
    assert c.get_pending_payout(ADDR_VENDOR_B) == 750  # 700 + 50 bond
    assert c.get_pending_payout(ADDR_BUYER) == 350     # 300 surplus + 50 forfeited ghost bond
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 4. Commitment Mismatch & Tampered Reveal Blocked
# -----------------------------------------------------------------------------
def test_commitment_mismatch_and_tampered_reveal_blocked():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-tamper", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)

    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-tamper", "b-b", 50, compute_bid_commitment("t-tamper", "b-b", ADDR_VENDOR_B, 650, "secret-b"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-tamper", "b-c", 50, compute_bid_commitment("t-tamper", "b-c", ADDR_VENDOR_C, 820, "secret-c"))

    # Wrong bidder tries to reveal VendorB's bid
    set_sender(ADDR_ATTACKER)
    with pytest.raises(UserError, match="UNAUTHORIZED_CALLER"):
        c.reveal_bid("t-tamper", "b-b", 650, "secret-b", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # VendorB tries to tamper price down to 649 after seeing other bids
    set_sender(ADDR_VENDOR_B)
    with pytest.raises(UserError, match="COMMITMENT_MISMATCH"):
        c.reveal_bid("t-tamper", "b-b", 649, "secret-b", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # VendorB tries wrong salt
    with pytest.raises(UserError, match="COMMITMENT_MISMATCH"):
        c.reveal_bid("t-tamper", "b-b", 650, "wrong-salt", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # Price above reserve blocked
    with pytest.raises(UserError, match="INVALID_BID_PRICE"):
        c.reveal_bid("t-tamper", "b-b", 1001, "secret-b", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # Exact original preimage succeeds
    assert c.reveal_bid("t-tamper", "b-b", 650, "secret-b", "GridVault", fixtures["b"][0], fixtures["b"][1]) == (
        "BID_REVEALED:t-tamper:b-b:PRICE_650"
    )
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 5. Retriable UNVERIFIABLE, Recovery & Safe No-Fault Unwind
# -----------------------------------------------------------------------------
def test_retriable_unverifiable_and_safe_no_fault_unwind():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-retry", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)

    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-retry", "b-b", 50, compute_bid_commitment("t-retry", "b-b", ADDR_VENDOR_B, 650, "sb"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-retry", "b-c", 50, compute_bid_commitment("t-retry", "b-c", ADDR_VENDOR_C, 820, "sc"))

    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("t-retry", "b-b", 650, "sb", "GridVault", fixtures["b"][0], fixtures["b"][1])
    set_sender(ADDR_VENDOR_C)
    c.reveal_bid("t-retry", "b-c", 820, "sc", "ApexStorage", fixtures["c"][0], fixtures["c"][1])

    # Simulate transient HTTP 503 on bid-b -> UNVERIFIABLE:FETCH_OR_PARSE_FAILURE (retriable!)
    orig_b_resp = mock_web_responses[fixtures["b"][0]]
    mock_web_responses[fixtures["b"][0]] = MockWebResponse(status=503, body=b"Service Unavailable")
    set_sender(ADDR_AUDITOR)
    assert c.evaluate_bid("t-retry", "b-b") == "EVALUATED:t-retry:b-b:UNVERIFIABLE:FETCH_OR_PARSE_FAILURE"

    # Award blocked while bid-b is UNVERIFIABLE
    with pytest.raises(UserError, match="UNEVALUATED_BID_EXISTS"):
        c.award_tender("t-retry")

    # Unwind blocked before MAX_EVAL_ATTEMPTS and before unwind_deadline
    with pytest.raises(UserError, match="UNWIND_CONDITIONS_NOT_MET"):
        c.unwind_unverifiable_tender("t-retry")

    # Attempt 2 recovers once HTTP 200 returns
    mock_web_responses[fixtures["b"][0]] = orig_b_resp
    assert c.evaluate_bid("t-retry", "b-b") == "EVALUATED:t-retry:b-b:ELIGIBLE:ALL_REQUIREMENTS_MET"
    assert json.loads(c.get_bid("t-retry", "b-b"))["eval_attempts"] == 2

    # Persistent HTTP 503 on bid-c for all MAX_EVAL_ATTEMPTS = 5 unlocks safe no-fault unwind
    mock_web_responses[fixtures["c"][0]] = MockWebResponse(status=503, body=b"Down")
    for attempt in range(1, MAX_EVAL_ATTEMPTS + 1):
        res = c.evaluate_bid("t-retry", "b-c")
        assert res == "EVALUATED:t-retry:b-c:UNVERIFIABLE:FETCH_OR_PARSE_FAILURE"
        assert json.loads(c.get_bid("t-retry", "b-c"))["eval_attempts"] == attempt

    # 6th attempt blocked
    with pytest.raises(UserError, match="MAX_EVAL_ATTEMPTS_REACHED"):
        c.evaluate_bid("t-retry", "b-c")

    # Safe no-fault unwind refunds 100% of reserve & all bid bonds
    assert c.unwind_unverifiable_tender("t-retry") == "UNWOUND_REFUNDED:t-retry"
    assert json.loads(c.get_tender("t-retry"))["status"] == "UNWOUND_REFUNDED"
    assert c.get_pending_payout(ADDR_BUYER) == 1000
    assert c.get_pending_payout(ADDR_VENDOR_B) == 50
    assert c.get_pending_payout(ADDR_VENDOR_C) == 50
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 6. Compatibility Table & Adversarial Byzantine Leader Rejection
# -----------------------------------------------------------------------------
def test_compatibility_table_and_adversarial_leader_rejection():
    # Pure normalization coerces invalid verdict/reason pairings to UNVERIFIABLE:AMBIGUOUS_DOSSIER
    for bad_v, bad_r in (
        ("ELIGIBLE", "SAFETY_CERT_MISSING"),
        ("DISQUALIFIED", "ALL_REQUIREMENTS_MET"),
        ("ELIGIBLE", "UNKNOWN_REASON"),
        ("BOGUS", "ALL_REQUIREMENTS_MET"),
    ):
        norm = normalize_bid_eval("t1", "b1", "ab" * 32, bad_v, bad_r)
        assert norm["verdict"] == "UNVERIFIABLE"
        assert norm["reason_code"] == "AMBIGUOUS_DOSSIER"

    val_env = normalize_bid_eval("t1", "b1", "ab" * 32, "ELIGIBLE", "ALL_REQUIREMENTS_MET")
    assert validate_leader_bid_eval(dict(val_env), val_env) is True
    assert validate_leader_bid_eval({**val_env, "reason_code": "SAFETY_CERT_MISSING"}, val_env) is False
    assert validate_leader_bid_eval({**val_env, "evidence_digest": "00" * 32}, val_env) is False
    assert validate_leader_bid_eval({**val_env, "extra_key": "x"}, val_env) is False

    # On-chain adversarial Byzantine leader mutation is rejected by validator_fn
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-byz", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 50)
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-byz", "b-b", 50, compute_bid_commitment("t-byz", "b-b", ADDR_VENDOR_B, 650, "sb"))
    set_time(1795000601)
    c.advance_to_reveal("t-byz")
    c.reveal_bid("t-byz", "b-b", 650, "sb", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # Leader returns ELIGIBLE with forged digest -> validator rejects!
    gl_mock.adversarial_leader_fn = lambda env: {**env, "evidence_digest": "ff" * 32}
    with pytest.raises(UserError, match="CONSENSUS_VALIDATION_FAILED"):
        c.evaluate_bid("t-byz", "b-b")

    # Leader returns forged reason_code with same verdict -> validator rejects!
    gl_mock.adversarial_leader_fn = lambda env: {**env, "reason_code": "SAFETY_CERT_MISSING"}
    with pytest.raises(UserError, match="CONSENSUS_VALIDATION_FAILED"):
        c.evaluate_bid("t-byz", "b-b")

    # Honest leader succeeds
    gl_mock.adversarial_leader_fn = None
    assert c.evaluate_bid("t-byz", "b-b") == "EVALUATED:t-byz:b-b:ELIGIBLE:ALL_REQUIREMENTS_MET"
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 7. Ceiling 5% Bid Bond Rounding & Zero-Stranded-Escrow Guards
# -----------------------------------------------------------------------------
def test_ceiling_bond_rounding_and_zero_stranded_escrow_guards():
    assert compute_min_bid_bond(100) == 10   # floor MIN_BID_BOND
    assert compute_min_bid_bond(200) == 10   # exact 5%
    assert compute_min_bid_bond(201) == 11   # ceiling(201 * 0.05) = 11
    assert compute_min_bid_bond(1000) == 50

    c = BlindTender()
    set_time(1795000000)

    # Zero deposit reverts cleanly
    set_sender(ADDR_BUYER)
    set_value(0)
    with pytest.raises(UserError, match="ZERO_DEPOSIT"):
        c.deposit_escrow()

    # Open tender without escrow reverts cleanly with zero stranded balance
    with pytest.raises(UserError, match="INSUFFICIENT_ESCROW_BALANCE"):
        c.open_tender("t-ceil", 201, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    assert c.balance == 0

    _deposit(c, ADDR_BUYER, 201)
    assert c.open_tender("t-ceil", 201, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX) == (
        "TENDER_OPENED:t-ceil:MIN_BOND_11"
    )

    # Buyer cannot bid on own tender
    _deposit(c, ADDR_BUYER, 20)
    set_sender(ADDR_BUYER)
    with pytest.raises(UserError, match="BUYER_CANNOT_BID"):
        c.commit_bid("t-ceil", "b-self", 11, compute_bid_commitment("t-ceil", "b-self", ADDR_BUYER, 150, "s"))

    # VendorA tries bond=10 (< ceiling 11) -> INSUFFICIENT_BID_BOND
    _deposit(c, ADDR_VENDOR_A, 20)
    set_sender(ADDR_VENDOR_A)
    h_a = compute_bid_commitment("t-ceil", "b-a", ADDR_VENDOR_A, 150, "sa")
    with pytest.raises(UserError, match="INSUFFICIENT_BID_BOND"):
        c.commit_bid("t-ceil", "b-a", 10, h_a)

    # Bond=11 succeeds; duplicate bid by same bidder blocked
    assert c.commit_bid("t-ceil", "b-a", 11, h_a) == "BID_COMMITTED:t-ceil:b-a:COMMIT_OPEN"
    with pytest.raises(UserError, match="BIDDER_ALREADY_COMMITTED"):
        c.commit_bid("t-ceil", "b-a2", 11, h_a)

    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 8. Unauthorized Callers, Phase Guards, Conclusive Immutability & Empty Cancel
# -----------------------------------------------------------------------------
def test_unauthorized_callers_phase_guards_and_conclusive_immutability():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    # Empty tender cancel guard
    _deposit(c, ADDR_BUYER, 500)
    c.open_tender("t-empty", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    with pytest.raises(UserError, match="COMMIT_WINDOW_STILL_ACTIVE"):
        c.cancel_empty_tender("t-empty")
    with pytest.raises(UserError, match="COMMIT_WINDOW_STILL_ACTIVE"):
        c.advance_to_reveal("t-empty")

    set_time(1795000601)
    with pytest.raises(UserError, match="NO_BIDS_COMMITTED"):
        c.advance_to_reveal("t-empty")
    assert c.cancel_empty_tender("t-empty") == "CANCELLED_EMPTY:t-empty"
    assert c.get_pending_payout(ADDR_BUYER) == 500

    # Active tender phase guards & conclusive immutability
    set_time(1795001000)
    c.open_tender("t-phase", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 25)
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-phase", "b-b", 25, compute_bid_commitment("t-phase", "b-b", ADDR_VENDOR_B, 400, "sb"))

    # Early reveal before REVEAL_OPEN fails
    with pytest.raises(UserError, match="REVEAL_PHASE_NOT_OPEN"):
        c.reveal_bid("t-phase", "b-b", 400, "sb", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # Advance after commit_deadline
    set_time(1795001601)
    assert c.advance_to_reveal("t-phase") == "REVEAL_OPENED:t-phase"
    c.reveal_bid("t-phase", "b-b", 400, "sb", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # Replay reveal blocked
    with pytest.raises(UserError, match="BID_ALREADY_REVEALED"):
        c.reveal_bid("t-phase", "b-b", 400, "sb", "GridVault", fixtures["b"][0], fixtures["b"][1])

    # Conclusive evaluation is immutable
    set_sender(ADDR_AUDITOR)
    assert c.evaluate_bid("t-phase", "b-b") == "EVALUATED:t-phase:b-b:ELIGIBLE:ALL_REQUIREMENTS_MET"
    with pytest.raises(UserError, match="BID_ALREADY_CONCLUDED"):
        c.evaluate_bid("t-phase", "b-b")

    # Award + replay award blocked
    assert c.award_tender("t-phase") == "AWARDED:t-phase:b-b:PRICE_400"
    with pytest.raises(UserError, match="INVALID_TENDER_STATE"):
        c.award_tender("t-phase")

    # Attacker with 0 payout cannot withdraw
    set_sender(ADDR_ATTACKER)
    with pytest.raises(UserError, match="NO_PENDING_PAYOUT"):
        c.withdraw_payout()
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 9. Repo Prefix Binding, Fail-Closed Evidence Fetch & NO_BLOCK_TIME
# -----------------------------------------------------------------------------
def test_repo_prefix_binding_and_fail_closed_evidence_and_no_block_time():
    c = BlindTender()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 500)
    # Invalid repo prefix rejected
    with pytest.raises(UserError, match="INVALID_REPO_PREFIX"):
        c.open_tender("t-bad", 500, 2, 600, 600, MANDATORY_REQS, "https://example.com/repo/")

    # Missing block clock fails closed
    set_time(None)
    with pytest.raises(UserError, match="NO_BLOCK_TIME"):
        c.open_tender("t-noclock", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)

    set_time(1795000000)
    c.open_tender("t-ev", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 25)
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-ev", "b-b", 25, compute_bid_commitment("t-ev", "b-b", ADDR_VENDOR_B, 400, "sb"))
    set_time(1795000601)
    c.advance_to_reveal("t-ev")

    valid_hash = "ab" * 32
    # Mutable /main/ branch URL rejected
    with pytest.raises(UserError, match="INVALID_EVIDENCE_URL"):
        c.reveal_bid(
            "t-ev", "b-b", 400, "sb", "GridVault",
            f"{REPO_PREFIX}main/evidence/bid.json", valid_hash,
        )

    # Foreign repo commit-pinned URL rejected
    with pytest.raises(UserError, match="REPO_PREFIX_MISMATCH"):
        c.reveal_bid(
            "t-ev", "b-b", 400, "sb", "GridVault",
            f"https://raw.githubusercontent.com/attacker/evil/{COMMIT_SHA}/evidence/bid.json", valid_hash,
        )

    # Valid commit-pinned URL under canonical repo prefix
    ev_url = f"{BASE_URL}edge_test.json"
    c.reveal_bid("t-ev", "b-b", 400, "sb", "GridVault edge", ev_url, valid_hash)

    # 1) HTTP 404 -> UNVERIFIABLE:FETCH_OR_PARSE_FAILURE
    mock_web_responses[ev_url] = MockWebResponse(status=404, body=b"Not Found")
    assert c.evaluate_bid("t-ev", "b-b") == "EVALUATED:t-ev:b-b:UNVERIFIABLE:FETCH_OR_PARSE_FAILURE"

    # 2) Empty body -> UNVERIFIABLE:FETCH_OR_PARSE_FAILURE
    mock_web_responses[ev_url] = MockWebResponse(status=200, body=b"")
    assert c.evaluate_bid("t-ev", "b-b") == "EVALUATED:t-ev:b-b:UNVERIFIABLE:FETCH_OR_PARSE_FAILURE"

    # 3) Oversized body (>2500 B) -> UNVERIFIABLE:FETCH_OR_PARSE_FAILURE
    mock_web_responses[ev_url] = MockWebResponse(status=200, body=b"x" * (MAX_EVIDENCE_BYTES + 1))
    assert c.evaluate_bid("t-ev", "b-b") == "EVALUATED:t-ev:b-b:UNVERIFIABLE:FETCH_OR_PARSE_FAILURE"

    # 4) Invalid UTF-8 bytes -> UNVERIFIABLE:FETCH_OR_PARSE_FAILURE
    mock_web_responses[ev_url] = MockWebResponse(status=200, body=b"\xff\xfe\xfd")
    assert c.evaluate_bid("t-ev", "b-b") == "EVALUATED:t-ev:b-b:UNVERIFIABLE:FETCH_OR_PARSE_FAILURE"

    # 5) Valid UTF-8 but SHA-256 mismatch -> UNVERIFIABLE:HASH_MISMATCH
    mock_web_responses[ev_url] = MockWebResponse(status=200, body=b'{"vendor":"tampered"}')
    assert c.evaluate_bid("t-ev", "b-b") == "EVALUATED:t-ev:b-b:UNVERIFIABLE:HASH_MISMATCH"
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 10. Global Accounting Conservation Invariant Across Full Multi-Tender Lifecycle
# -----------------------------------------------------------------------------
def test_accounting_conservation_invariant_across_full_lifecycle():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    # Check deterministic tie-breaker on equal eligible prices (bid_id ascending)
    tie_winner, tie_price = select_vickrey_winner(
        [
            {"bid_id": "bid-z", "price": 500, "eval_verdict": "ELIGIBLE"},
            {"bid_id": "bid-a", "price": 500, "eval_verdict": "ELIGIBLE"},
        ]
    )
    assert tie_winner["bid_id"] == "bid-a"
    assert tie_price == 500

    # Tender 1: Awarded with 2 eligible bids + 1 disqualified
    _deposit(c, ADDR_BUYER, 2000)
    _assert_conservation(c)
    c.open_tender("inv-1", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _assert_conservation(c)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)
    _assert_conservation(c)

    set_sender(ADDR_VENDOR_B)
    c.commit_bid("inv-1", "b1", 50, compute_bid_commitment("inv-1", "b1", ADDR_VENDOR_B, 600, "s1"))
    _assert_conservation(c)
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("inv-1", "b2", 50, compute_bid_commitment("inv-1", "b2", ADDR_VENDOR_C, 750, "s2"))
    _assert_conservation(c)

    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("inv-1", "b1", 600, "s1", "GridVault", fixtures["b"][0], fixtures["b"][1])
    set_sender(ADDR_VENDOR_C)
    c.reveal_bid("inv-1", "b2", 750, "s2", "ApexStorage", fixtures["c"][0], fixtures["c"][1])
    c.evaluate_bid("inv-1", "b1")
    c.evaluate_bid("inv-1", "b2")
    c.award_tender("inv-1")
    _assert_conservation(c)

    # Tender 2: Unwound via timeout past unwind_deadline
    set_sender(ADDR_BUYER)
    c.open_tender("inv-2", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_A, 50)
    set_sender(ADDR_VENDOR_A)
    c.commit_bid("inv-2", "a1", 50, compute_bid_commitment("inv-2", "a1", ADDR_VENDOR_A, 500, "sa"))
    set_time(1795000601)
    c.advance_to_reveal("inv-2")
    set_time(1795000601 + 600 + ADJUDICATION_GRACE_SECONDS + 1)
    assert c.unwind_unverifiable_tender("inv-2") == "UNWOUND_REFUNDED:inv-2"
    _assert_conservation(c)

    # Everyone withdraws -> zero residual balance
    for addr in (ADDR_BUYER, ADDR_VENDOR_A, ADDR_VENDOR_B, ADDR_VENDOR_C):
        if c.get_pending_payout(addr) > 0:
            set_sender(addr)
            c.withdraw_payout()
            _assert_conservation(c)

    final_acc = _assert_conservation(c)
    assert final_acc["contract_balance"] == 0
    assert final_acc["total_locked_reserves"] == 0
    assert final_acc["total_locked_bonds"] == 0
    assert final_acc["total_pending_withdrawals"] == 0
    assert final_acc["total_deposited"] == final_acc["total_withdrawn"] == 2150
