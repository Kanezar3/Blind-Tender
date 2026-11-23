# GenLayer Portal Submission Note — Blind Tender

## Title
Blind Tender — Sealed-Bid Vickrey Reverse Auction with Price-Blind Consensus Eligibility

## Notes / Description (<= 1,000 characters)
Blind Tender is a GenLayer Intelligent Contract implementing a commit-reveal sealed-bid Vickrey reverse auction with price-blind consensus eligibility for utility grid-scale BESS procurement. Vendors lock ceiling-5% native bid bonds and SHA-256 price commitments via a zero-stranding escrow pattern. After reveal, validators fetch commit-pinned technical dossiers without ever seeing bid prices to classify compliance into disjoint ELIGIBLE/DISQUALIFIED/UNVERIFIABLE reason codes, with retriable UNVERIFIABLE unwind and Byzantine leader checks. Deterministic code awards the lowest-price ELIGIBLE bid at the second-lowest ELIGIBLE price. Deployed byte-identically on StudioNet (0xfE39e345c7497B913E6143f629dDfFDc2E26be75), where live consensus disqualified a 417-wei lowballer missing UL-9540A certification and awarded the 673-wei compliant bid at the 829-wei Vickrey price across 28 finalized txs (10/10 pytest, 9 live abuse guards, final balance = 0).

## Evidence & Supporting Information URLs
1. Repository: `https://github.com/Kanezar3/Blind-Tender`
2. Pinned Contract Source: `https://github.com/Kanezar3/Blind-Tender/blob/2fbb47405ace9afc0eda74f86c53a0fa48b3f207/contracts/blind_tender.py`
3. StudioNet Contract Explorer: `https://explorer-studio.genlayer.com/address/0xfE39e345c7497B913E6143f629dDfFDc2E26be75`
4. Release Manifest: `https://github.com/Kanezar3/Blind-Tender/blob/main/release_manifest.json`
