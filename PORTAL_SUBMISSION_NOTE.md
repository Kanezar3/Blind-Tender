# GenLayer Portal Submission Note — Blind Tender

## Title
Blind Tender — Sealed-Bid Vickrey Reverse Auction with Price-Blind Consensus Eligibility

## Notes / Description (<= 1,000 characters)
Blind Tender is a domain-reusable commit-reveal sealed-bid Vickrey reverse auction with price-blind consensus eligibility. Vendors lock ceiling-5% native bid bonds and SHA-256 commitments binding BOTH hidden price and dossier digest. Validators fetch commit-pinned unstructured prose dossiers without seeing prices or unverified summaries to classify compliance into disjoint reason codes with exact leader/validator equality; deterministic code awards the lowest-price ELIGIBLE bid at the second-lowest ELIGIBLE price. Live byte-identically on StudioNet (0x33e1CA90d4ff4E58ECed6De49d40A4ee155356c4, 31 finalized txs): consensus disqualified a 417-wei lowballer missing UL-9540A certification and awarded the 673-wei compliant bid at 829 wei (contract balance = 0), blocking spoofed reveals, price/dossier-swap tampering, premature award, and replay (11 live guards). Does not prove mainnet capital, multi-repo hosting, or live deadline-expiry slashing (unit-tested in 10/10 pytest).

## Evidence & Supporting Information URLs
1. StudioNet Contract Explorer: `https://explorer-studio.genlayer.com/address/0x33e1CA90d4ff4E58ECed6De49d40A4ee155356c4`
2. Repository: `https://github.com/Kanezar3/Blind-Tender`
3. Pinned Contract Source (`780698d`): `https://github.com/Kanezar3/Blind-Tender/blob/780698d213d0673c148210a0a3766574bf3e6eb2/contracts/blind_tender.py`
4. Pinned Release Manifest (`edec7d5`): `https://github.com/Kanezar3/Blind-Tender/blob/edec7d5d9f1c1a3bfb27ec165a6c5c8fc055547f/release_manifest.json`
5. Key Demo Tx — Price-Blind Disqualification of 417-wei Lowballer (`S4A`): `https://explorer-studio.genlayer.com/tx/0xbf097299382277a736849f2ad38e710a60ea558c84de64332d3b7b27af4b0b57`
6. Key Demo Tx — Price-Blind Eligibility of 673-wei Winner (`S4B`): `https://explorer-studio.genlayer.com/tx/0x66a0a3cb914cd173a82084beafb0bed1108f4eacc7ba260f7d8fc8e85724c9e6`
7. Key Demo Tx — Deterministic Vickrey Award at 829 wei (`S5`): `https://explorer-studio.genlayer.com/tx/0x457faa4d254fcb801d285870667e8825633943fd02b10f037d900a66e68f8496`
8. Key Demo Tx — Winner Native Wei Withdrawal (`879` wei, `S6A`): `https://explorer-studio.genlayer.com/tx/0x6b6be911f9752fffd8032bb75da4e1fbc8491ab190f8cfe469e69dce453325d5`
9. Expected Failure — Unauthorized Reveal (`X4`): `https://explorer-studio.genlayer.com/tx/0x19d205e3f3195d7e5fbc4fe11e4a0ae5c1e0ae6a4ec54c54f0c1b8e24fec6bfd`
10. Expected Failure — Tampered Price Reveal (`X5`): `https://explorer-studio.genlayer.com/tx/0x954f7114fd0c3cfa459414121cb5454573337e3a3bb822d1b6b47496cadf99cc`
11. Expected Failure — Post-Reveal Dossier Swap (`X6`): `https://explorer-studio.genlayer.com/tx/0xd4aee976d79a1f3474b928bb3f24a8a3a08745d17cec246f14a7d7e263a22e62`
12. Expected Failure — Premature Award Before Evaluation (`X9`): `https://explorer-studio.genlayer.com/tx/0x86d38f4cfff981bee04f5d681f97aa62f04c5a7156625c342aefe0e39f2bca30`
13. Expected Failure — Replay Concluded Evaluation (`X10`): `https://explorer-studio.genlayer.com/tx/0xb7e9b5d827a516135ab1c30426c6daece7eb89fcdccbe8e84e8803dcdf43a71f`
