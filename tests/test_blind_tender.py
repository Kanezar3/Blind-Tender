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
    text = raw.decode("utf-8")
    # Verify natural prose fixtures contain no internal verdict/reason tokens
    for token in (
        "ELIGIBLE",
        "DISQUALIFIED",
        "UNVERIFIABLE",
        "COMPLIANCE_CERT_MISSING",
        "TECHNICAL_SPEC_UNMET",
        "ALL_REQUIREMENTS_MET",
        "HASH_MISMATCH",
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
        ("a", "bid_a_lowball_no_cert.txt"),
        ("b", "bid_b_compliant_winner.txt"),
        ("c", "bid_c_compliant_runnerup.txt"),
    ):
        url, raw, digest = _load_fixture(fname)
        mock_web_responses[url] = MockWebResponse(status=200, body=raw)
        out[key] = (url, digest)

    mock_llm_dict["LOWCOST CELLS LTD."] = json.dumps(
        {"verdict": "DISQUALIFIED", "reason_code": "COMPLIANCE_CERT_MISSING"}
    )
    mock_llm_dict["GRIDVAULT ENERGY CORP."] = json.dumps(
        {"verdict": "ELIGIBLE", "reason_code": "ALL_REQUIREMENTS_MET"}
    )
    mock_llm_dict["APEXSTORAGE SYSTEMS AG"] = json.dumps(
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

    _deposit(c, ADDR_BUYER, 1000)
    res = c.open_tender("bess-2026-01", 1000, 3, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    assert res == "TENDER_OPENED:bess-2026-01:MIN_BOND_50"

    _deposit(c, ADDR_VENDOR_A, 50)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)

    h_a = compute_bid_commitment("bess-2026-01", "bid-a", ADDR_VENDOR_A, 417, fixtures["a"][1], "salt-a")
    h_b = compute_bid_commitment("bess-2026-01", "bid-b", ADDR_VENDOR_B, 673, fixtures["b"][1], "salt-b")
    h_c = compute_bid_commitment("bess-2026-01", "bid-c", ADDR_VENDOR_C, 829, fixtures["c"][1], "salt-c")

    set_sender(ADDR_VENDOR_A)
    assert c.commit_bid("bess-2026-01", "bid-a", 50, h_a) == "BID_COMMITTED:bess-2026-01:bid-a:COMMIT_OPEN"
    set_sender(ADDR_VENDOR_B)
    assert c.commit_bid("bess-2026-01", "bid-b", 50, h_b) == "BID_COMMITTED:bess-2026-01:bid-b:COMMIT_OPEN"
    set_sender(ADDR_VENDOR_C)
    assert c.commit_bid("bess-2026-01", "bid-c", 50, h_c) == "BID_COMMITTED:bess-2026-01:bid-c:REVEAL_OPEN"

    set_time(1795000100)
    set_sender(ADDR_VENDOR_A)
    assert c.reveal_bid("bess-2026-01", "bid-a", 417, "salt-a", fixtures["a"][0], fixtures["a"][1]) == (
        "BID_REVEALED:bess-2026-01:bid-a:PRICE_417"
    )
    set_sender(ADDR_VENDOR_B)
    assert c.reveal_bid("bess-2026-01", "bid-b", 673, "salt-b", fixtures["b"][0], fixtures["b"][1]) == (
        "BID_REVEALED:bess-2026-01:bid-b:PRICE_673"
    )
    set_sender(ADDR_VENDOR_C)
    assert c.reveal_bid("bess-2026-01", "bid-c", 829, "salt-c", fixtures["c"][0], fixtures["c"][1]) == (
        "BID_REVEALED:bess-2026-01:bid-c:PRICE_829"
    )

    set_sender(ADDR_AUDITOR)
    assert c.evaluate_bid("bess-2026-01", "bid-a") == "EVALUATED:bess-2026-01:bid-a:DISQUALIFIED:COMPLIANCE_CERT_MISSING"
    assert c.evaluate_bid("bess-2026-01", "bid-b") == "EVALUATED:bess-2026-01:bid-b:ELIGIBLE:ALL_REQUIREMENTS_MET"
    assert c.evaluate_bid("bess-2026-01", "bid-c") == "EVALUATED:bess-2026-01:bid-c:ELIGIBLE:ALL_REQUIREMENTS_MET"

    # Price-blind & summary-free isolation proof
    assert len(exec_prompt_call_history) >= 6
    for prompt in exec_prompt_call_history:
        assert "417" not in prompt
        assert "673" not in prompt
        assert "829" not in prompt
        assert "VENDOR_SUMMARY" not in prompt

    assert c.award_tender("bess-2026-01") == "AWARDED:bess-2026-01:bid-b:PRICE_829"

    tender = json.loads(c.get_tender("bess-2026-01"))
    assert tender["status"] == "AWARDED"
    assert tender["winner_bid_id"] == "bid-b"
    assert tender["winner_bidder"] == str(ADDR_VENDOR_B)
    assert tender["clearing_price"] == 829

    assert c.get_pending_payout(ADDR_VENDOR_B) == 879
    assert c.get_pending_payout(ADDR_BUYER) == 171
    assert c.get_pending_payout(ADDR_VENDOR_A) == 50
    assert c.get_pending_payout(ADDR_VENDOR_C) == 50

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
# 2. Sole Eligible Clears at Own Price & All-Disqualified -> CLOSED_NO_WINNER
# -----------------------------------------------------------------------------
def test_sole_eligible_bidder_clears_at_own_price_and_no_winner_refunds_buyer():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-sole", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_A, 50)
    _deposit(c, ADDR_VENDOR_B, 50)

    set_sender(ADDR_VENDOR_A)
    c.commit_bid("t-sole", "b-a", 50, compute_bid_commitment("t-sole", "b-a", ADDR_VENDOR_A, 417, fixtures["a"][1], "sa"))
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-sole", "b-b", 50, compute_bid_commitment("t-sole", "b-b", ADDR_VENDOR_B, 673, fixtures["b"][1], "sb"))

    set_sender(ADDR_VENDOR_A)
    c.reveal_bid("t-sole", "b-a", 417, "sa", fixtures["a"][0], fixtures["a"][1])
    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("t-sole", "b-b", 673, "sb", fixtures["b"][0], fixtures["b"][1])

    set_sender(ADDR_AUDITOR)
    c.evaluate_bid("t-sole", "b-a")
    c.evaluate_bid("t-sole", "b-b")
    assert c.award_tender("t-sole") == "AWARDED:t-sole:b-b:PRICE_673"
    assert c.get_pending_payout(ADDR_VENDOR_B) == 723
    assert c.get_pending_payout(ADDR_BUYER) == 327
    assert c.get_pending_payout(ADDR_VENDOR_A) == 50

    _deposit(c, ADDR_BUYER, 500)
    c.open_tender("t-none", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_A, 25)
    _deposit(c, ADDR_VENDOR_C, 25)

    set_sender(ADDR_VENDOR_A)
    c.commit_bid("t-none", "n-a", 25, compute_bid_commitment("t-none", "n-a", ADDR_VENDOR_A, 300, fixtures["a"][1], "s1"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-none", "n-c", 25, compute_bid_commitment("t-none", "n-c", ADDR_VENDOR_C, 400, fixtures["a"][1], "s2"))

    set_sender(ADDR_VENDOR_A)
    c.reveal_bid("t-none", "n-a", 300, "s1", fixtures["a"][0], fixtures["a"][1])
    set_sender(ADDR_VENDOR_C)
    c.reveal_bid("t-none", "n-c", 400, "s2", fixtures["a"][0], fixtures["a"][1])

    set_sender(ADDR_AUDITOR)
    c.evaluate_bid("t-none", "n-a")
    c.evaluate_bid("t-none", "n-c")
    assert c.award_tender("t-none") == "CLOSED_NO_WINNER:t-none"
    assert json.loads(c.get_tender("t-none"))["status"] == "CLOSED_NO_WINNER"
    assert c.get_pending_payout(ADDR_BUYER) == 327 + 500
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
    c.commit_bid("t-ghost", "b-b", 50, compute_bid_commitment("t-ghost", "b-b", ADDR_VENDOR_B, 700, fixtures["b"][1], "sb"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-ghost", "b-c", 50, compute_bid_commitment("t-ghost", "b-c", ADDR_VENDOR_C, 600, fixtures["c"][1], "sc"))

    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("t-ghost", "b-b", 700, "sb", fixtures["b"][0], fixtures["b"][1])
    set_sender(ADDR_AUDITOR)
    c.evaluate_bid("t-ghost", "b-b")

    with pytest.raises(UserError, match="UNREVEALED_BIDS_PENDING"):
        c.award_tender("t-ghost")

    set_time(1795000601)
    assert c.award_tender("t-ghost") == "AWARDED:t-ghost:b-b:PRICE_700"
    ghost_bid = json.loads(c.get_bid("t-ghost", "b-c"))
    assert ghost_bid["status"] == "FORFEITED_UNREVEALED"
    assert c.get_pending_payout(ADDR_VENDOR_C) == 0
    assert c.get_pending_payout(ADDR_VENDOR_B) == 750
    assert c.get_pending_payout(ADDR_BUYER) == 350
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 4. Commitment Binds BOTH Price & Dossier Hash (Blocks Price Tampering & Dossier Swap)
# -----------------------------------------------------------------------------
def test_commitment_mismatch_blocks_price_tamper_and_dossier_swap():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-tamper", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)

    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-tamper", "b-b", 50, compute_bid_commitment("t-tamper", "b-b", ADDR_VENDOR_B, 673, fixtures["b"][1], "secret-b"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-tamper", "b-c", 50, compute_bid_commitment("t-tamper", "b-c", ADDR_VENDOR_C, 829, fixtures["c"][1], "secret-c"))

    # Wrong bidder tries to reveal VendorB's bid
    set_sender(ADDR_ATTACKER)
    with pytest.raises(UserError, match="UNAUTHORIZED_CALLER"):
        c.reveal_bid("t-tamper", "b-b", 673, "secret-b", fixtures["b"][0], fixtures["b"][1])

    # VendorB tampers price -> COMMITMENT_MISMATCH
    set_sender(ADDR_VENDOR_B)
    with pytest.raises(UserError, match="COMMITMENT_MISMATCH"):
        c.reveal_bid("t-tamper", "b-b", 600, "secret-b", fixtures["b"][0], fixtures["b"][1])

    # VendorB tries to swap in non-compliant dossier A to back out -> COMMITMENT_MISMATCH!
    with pytest.raises(UserError, match="COMMITMENT_MISMATCH"):
        c.reveal_bid("t-tamper", "b-b", 673, "secret-b", fixtures["a"][0], fixtures["a"][1])

    # Wrong salt -> COMMITMENT_MISMATCH
    with pytest.raises(UserError, match="COMMITMENT_MISMATCH"):
        c.reveal_bid("t-tamper", "b-b", 673, "wrong-salt", fixtures["b"][0], fixtures["b"][1])

    # Price above reserve blocked
    with pytest.raises(UserError, match="INVALID_BID_PRICE"):
        c.reveal_bid("t-tamper", "b-b", 1001, "secret-b", fixtures["b"][0], fixtures["b"][1])

    # Exact committed preimage succeeds
    assert c.reveal_bid("t-tamper", "b-b", 673, "secret-b", fixtures["b"][0], fixtures["b"][1]) == (
        "BID_REVEALED:t-tamper:b-b:PRICE_673"
    )
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 5. Anti-Griefing: Single 404 Bidder Cannot Veto Auction; Systemic Outage Unwinds
# -----------------------------------------------------------------------------
def test_anti_griefing_single_bad_bidder_forfeits_and_systemic_outage_unwinds():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    # Case A: VendorC submits a 404 link trying to abort the auction after VendorB is ELIGIBLE
    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-grief", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)

    dead_url = f"{BASE_URL}dead_404.txt"
    dead_hash = "aa" * 32
    mock_web_responses[dead_url] = MockWebResponse(status=404, body=b"Not Found")

    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-grief", "b-b", 50, compute_bid_commitment("t-grief", "b-b", ADDR_VENDOR_B, 673, fixtures["b"][1], "sb"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-grief", "b-c", 50, compute_bid_commitment("t-grief", "b-c", ADDR_VENDOR_C, 500, dead_hash, "sc"))

    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("t-grief", "b-b", 673, "sb", fixtures["b"][0], fixtures["b"][1])
    set_sender(ADDR_VENDOR_C)
    c.reveal_bid("t-grief", "b-c", 500, "sc", dead_url, dead_hash)

    set_sender(ADDR_AUDITOR)
    assert c.evaluate_bid("t-grief", "b-b") == "EVALUATED:t-grief:b-b:ELIGIBLE:ALL_REQUIREMENTS_MET"

    # Exhaust 5 attempts on VendorC's 404 link
    for attempt in range(1, MAX_EVAL_ATTEMPTS + 1):
        assert c.evaluate_bid("t-grief", "b-c") == "EVALUATED:t-grief:b-c:UNVERIFIABLE:FETCH_OR_PARSE_FAILURE"
        if attempt < MAX_EVAL_ATTEMPTS:
            with pytest.raises(UserError, match="UNEVALUATED_BID_EXISTS"):
                c.award_tender("t-grief")

    # 6th attempt blocked
    with pytest.raises(UserError, match="MAX_EVAL_ATTEMPTS_REACHED"):
        c.evaluate_bid("t-grief", "b-c")

    # Attacker CANNOT unwind the tender because VendorB succeeded!
    with pytest.raises(UserError, match="UNWIND_CONDITIONS_NOT_MET"):
        c.unwind_unverifiable_tender("t-grief")

    # Instead, award_tender proceeds: VendorB wins at 673, VendorC forfeits 50 bond to Buyer!
    assert c.award_tender("t-grief") == "AWARDED:t-grief:b-b:PRICE_673"
    assert json.loads(c.get_bid("t-grief", "b-c"))["status"] == "FORFEITED_UNVERIFIABLE"
    assert c.get_pending_payout(ADDR_VENDOR_C) == 0
    assert c.get_pending_payout(ADDR_VENDOR_B) == 723
    assert c.get_pending_payout(ADDR_BUYER) == 327 + 50  # surplus + forfeited griefer bond
    _assert_conservation(c)

    # Case B: Systemic outage where ALL revealed bids exhaust 5 UNVERIFIABLE attempts -> safe unwind
    _deposit(c, ADDR_BUYER, 500)
    c.open_tender("t-outage", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_A, 25)
    _deposit(c, ADDR_VENDOR_C, 25)

    set_sender(ADDR_VENDOR_A)
    c.commit_bid("t-outage", "o-a", 25, compute_bid_commitment("t-outage", "o-a", ADDR_VENDOR_A, 300, dead_hash, "s1"))
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("t-outage", "o-c", 25, compute_bid_commitment("t-outage", "o-c", ADDR_VENDOR_C, 400, dead_hash, "s2"))

    set_sender(ADDR_VENDOR_A)
    c.reveal_bid("t-outage", "o-a", 300, "s1", dead_url, dead_hash)
    set_sender(ADDR_VENDOR_C)
    c.reveal_bid("t-outage", "o-c", 400, "s2", dead_url, dead_hash)

    set_sender(ADDR_AUDITOR)
    for _ in range(MAX_EVAL_ATTEMPTS):
        c.evaluate_bid("t-outage", "o-a")
        c.evaluate_bid("t-outage", "o-c")

    assert c.unwind_unverifiable_tender("t-outage") == "UNWOUND_REFUNDED:t-outage"
    assert c.get_pending_payout(ADDR_VENDOR_A) == 25
    assert c.get_pending_payout(ADDR_VENDOR_C) == 25
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 6. Compatibility Table & Adversarial Byzantine Leader Rejection
# -----------------------------------------------------------------------------
def test_compatibility_table_and_adversarial_leader_rejection():
    for bad_v, bad_r in (
        ("ELIGIBLE", "COMPLIANCE_CERT_MISSING"),
        ("DISQUALIFIED", "ALL_REQUIREMENTS_MET"),
        ("ELIGIBLE", "UNKNOWN_REASON"),
        ("BOGUS", "ALL_REQUIREMENTS_MET"),
    ):
        norm = normalize_bid_eval("t1", "b1", "ab" * 32, bad_v, bad_r)
        assert norm["verdict"] == "UNVERIFIABLE"
        assert norm["reason_code"] == "AMBIGUOUS_DOSSIER"

    val_env = normalize_bid_eval("t1", "b1", "ab" * 32, "ELIGIBLE", "ALL_REQUIREMENTS_MET")
    assert validate_leader_bid_eval(dict(val_env), val_env) is True
    assert validate_leader_bid_eval({**val_env, "reason_code": "COMPLIANCE_CERT_MISSING"}, val_env) is False
    assert validate_leader_bid_eval({**val_env, "evidence_digest": "00" * 32}, val_env) is False
    assert validate_leader_bid_eval({**val_env, "extra_key": "x"}, val_env) is False

    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 1000)
    c.open_tender("t-byz", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 50)
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-byz", "b-b", 50, compute_bid_commitment("t-byz", "b-b", ADDR_VENDOR_B, 673, fixtures["b"][1], "sb"))
    set_time(1795000601)
    c.advance_to_reveal("t-byz")
    c.reveal_bid("t-byz", "b-b", 673, "sb", fixtures["b"][0], fixtures["b"][1])

    gl_mock.adversarial_leader_fn = lambda env: {**env, "evidence_digest": "ff" * 32}
    with pytest.raises(UserError, match="CONSENSUS_VALIDATION_FAILED"):
        c.evaluate_bid("t-byz", "b-b")

    gl_mock.adversarial_leader_fn = lambda env: {**env, "reason_code": "COMPLIANCE_CERT_MISSING"}
    with pytest.raises(UserError, match="CONSENSUS_VALIDATION_FAILED"):
        c.evaluate_bid("t-byz", "b-b")

    gl_mock.adversarial_leader_fn = None
    assert c.evaluate_bid("t-byz", "b-b") == "EVALUATED:t-byz:b-b:ELIGIBLE:ALL_REQUIREMENTS_MET"
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 7. Ceiling 5% Bid Bond Rounding & Zero-Stranded-Escrow Guards
# -----------------------------------------------------------------------------
def test_ceiling_bond_rounding_and_zero_stranded_escrow_guards():
    assert compute_min_bid_bond(100) == 10
    assert compute_min_bid_bond(200) == 10
    assert compute_min_bid_bond(201) == 11
    assert compute_min_bid_bond(1000) == 50

    c = BlindTender()
    set_time(1795000000)

    set_sender(ADDR_BUYER)
    set_value(0)
    with pytest.raises(UserError, match="ZERO_DEPOSIT"):
        c.deposit_escrow()

    with pytest.raises(UserError, match="INSUFFICIENT_ESCROW_BALANCE"):
        c.open_tender("t-ceil", 201, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    assert c.balance == 0

    _deposit(c, ADDR_BUYER, 201)
    assert c.open_tender("t-ceil", 201, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX) == (
        "TENDER_OPENED:t-ceil:MIN_BOND_11"
    )

    _deposit(c, ADDR_BUYER, 20)
    set_sender(ADDR_BUYER)
    h_self = compute_bid_commitment("t-ceil", "b-self", ADDR_BUYER, 150, "ab" * 32, "s")
    with pytest.raises(UserError, match="BUYER_CANNOT_BID"):
        c.commit_bid("t-ceil", "b-self", 11, h_self)

    _deposit(c, ADDR_VENDOR_A, 20)
    set_sender(ADDR_VENDOR_A)
    h_a = compute_bid_commitment("t-ceil", "b-a", ADDR_VENDOR_A, 150, "ab" * 32, "sa")
    with pytest.raises(UserError, match="INSUFFICIENT_BID_BOND"):
        c.commit_bid("t-ceil", "b-a", 10, h_a)

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

    set_time(1795001000)
    c.open_tender("t-phase", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 25)
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-phase", "b-b", 25, compute_bid_commitment("t-phase", "b-b", ADDR_VENDOR_B, 400, fixtures["b"][1], "sb"))

    with pytest.raises(UserError, match="REVEAL_PHASE_NOT_OPEN"):
        c.reveal_bid("t-phase", "b-b", 400, "sb", fixtures["b"][0], fixtures["b"][1])

    set_time(1795001601)
    assert c.advance_to_reveal("t-phase") == "REVEAL_OPENED:t-phase"
    c.reveal_bid("t-phase", "b-b", 400, "sb", fixtures["b"][0], fixtures["b"][1])

    with pytest.raises(UserError, match="BID_ALREADY_REVEALED"):
        c.reveal_bid("t-phase", "b-b", 400, "sb", fixtures["b"][0], fixtures["b"][1])

    set_sender(ADDR_AUDITOR)
    assert c.evaluate_bid("t-phase", "b-b") == "EVALUATED:t-phase:b-b:ELIGIBLE:ALL_REQUIREMENTS_MET"
    with pytest.raises(UserError, match="BID_ALREADY_CONCLUDED"):
        c.evaluate_bid("t-phase", "b-b")

    assert c.award_tender("t-phase") == "AWARDED:t-phase:b-b:PRICE_400"
    with pytest.raises(UserError, match="INVALID_TENDER_STATE"):
        c.award_tender("t-phase")

    set_sender(ADDR_ATTACKER)
    with pytest.raises(UserError, match="NO_PENDING_PAYOUT"):
        c.withdraw_payout()
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 9. Repo Prefix Binding, Fail-Closed Fetch, HASH_MISMATCH Slashing & NO_BLOCK_TIME
# -----------------------------------------------------------------------------
def test_repo_prefix_binding_fail_closed_fetch_and_hash_mismatch_slashing():
    c = BlindTender()
    set_time(1795000000)

    _deposit(c, ADDR_BUYER, 500)
    with pytest.raises(UserError, match="INVALID_REPO_PREFIX"):
        c.open_tender("t-bad", 500, 2, 600, 600, MANDATORY_REQS, "https://example.com/repo/")

    set_time(None)
    with pytest.raises(UserError, match="NO_BLOCK_TIME"):
        c.open_tender("t-noclock", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)

    set_time(1795000000)
    c.open_tender("t-ev", 500, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_B, 25)
    valid_hash = "ab" * 32
    set_sender(ADDR_VENDOR_B)
    c.commit_bid("t-ev", "b-b", 25, compute_bid_commitment("t-ev", "b-b", ADDR_VENDOR_B, 400, valid_hash, "sb"))
    set_time(1795000601)
    c.advance_to_reveal("t-ev")

    with pytest.raises(UserError, match="INVALID_EVIDENCE_URL"):
        c.reveal_bid("t-ev", "b-b", 400, "sb", f"{REPO_PREFIX}main/evidence/bid.txt", valid_hash)

    with pytest.raises(UserError, match="REPO_PREFIX_MISMATCH"):
        c.reveal_bid(
            "t-ev", "b-b", 400, "sb",
            f"https://raw.githubusercontent.com/attacker/evil/{COMMIT_SHA}/evidence/bid.txt", valid_hash,
        )

    ev_url = f"{BASE_URL}edge_test.txt"
    c.reveal_bid("t-ev", "b-b", 400, "sb", ev_url, valid_hash)

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

    # 5) Valid UTF-8 but SHA-256 mismatch -> conclusive DISQUALIFIED:HASH_MISMATCH & bond forfeited!
    mock_web_responses[ev_url] = MockWebResponse(status=200, body=b"tampered dossier bytes")
    assert c.evaluate_bid("t-ev", "b-b") == "EVALUATED:t-ev:b-b:DISQUALIFIED:HASH_MISMATCH"
    with pytest.raises(UserError, match="BID_ALREADY_CONCLUDED"):
        c.evaluate_bid("t-ev", "b-b")

    assert c.award_tender("t-ev") == "CLOSED_NO_WINNER:t-ev"
    assert json.loads(c.get_bid("t-ev", "b-b"))["status"] == "FORFEITED_HASH_MISMATCH"
    assert c.get_pending_payout(ADDR_VENDOR_B) == 0
    assert c.get_pending_payout(ADDR_BUYER) == 525  # 500 reserve + 25 slashed bond
    _assert_conservation(c)


# -----------------------------------------------------------------------------
# 10. Global Accounting Conservation Invariant Across Full Multi-Tender Lifecycle
# -----------------------------------------------------------------------------
def test_accounting_conservation_invariant_across_full_lifecycle():
    c = BlindTender()
    fixtures = _register_default_fixtures()
    set_time(1795000000)

    tie_winner, tie_price = select_vickrey_winner(
        [
            {"bid_id": "bid-z", "price": 500, "eval_verdict": "ELIGIBLE"},
            {"bid_id": "bid-a", "price": 500, "eval_verdict": "ELIGIBLE"},
        ]
    )
    assert tie_winner["bid_id"] == "bid-a"
    assert tie_price == 500

    _deposit(c, ADDR_BUYER, 2000)
    _assert_conservation(c)
    c.open_tender("inv-1", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _assert_conservation(c)
    _deposit(c, ADDR_VENDOR_B, 50)
    _deposit(c, ADDR_VENDOR_C, 50)
    _assert_conservation(c)

    set_sender(ADDR_VENDOR_B)
    c.commit_bid("inv-1", "b1", 50, compute_bid_commitment("inv-1", "b1", ADDR_VENDOR_B, 600, fixtures["b"][1], "s1"))
    _assert_conservation(c)
    set_sender(ADDR_VENDOR_C)
    c.commit_bid("inv-1", "b2", 50, compute_bid_commitment("inv-1", "b2", ADDR_VENDOR_C, 750, fixtures["c"][1], "s2"))
    _assert_conservation(c)

    set_sender(ADDR_VENDOR_B)
    c.reveal_bid("inv-1", "b1", 600, "s1", fixtures["b"][0], fixtures["b"][1])
    set_sender(ADDR_VENDOR_C)
    c.reveal_bid("inv-1", "b2", 750, "s2", fixtures["c"][0], fixtures["c"][1])
    c.evaluate_bid("inv-1", "b1")
    c.evaluate_bid("inv-1", "b2")
    c.award_tender("inv-1")
    _assert_conservation(c)

    set_sender(ADDR_BUYER)
    c.open_tender("inv-2", 1000, 2, 600, 600, MANDATORY_REQS, REPO_PREFIX)
    _deposit(c, ADDR_VENDOR_A, 50)
    set_sender(ADDR_VENDOR_A)
    c.commit_bid("inv-2", "a1", 50, compute_bid_commitment("inv-2", "a1", ADDR_VENDOR_A, 500, fixtures["a"][1], "sa"))
    set_time(1795000601)
    c.advance_to_reveal("inv-2")
    set_time(1795000601 + 600 + ADJUDICATION_GRACE_SECONDS + 1)
    assert c.unwind_unverifiable_tender("inv-2") == "UNWOUND_REFUNDED:inv-2"
    _assert_conservation(c)

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
