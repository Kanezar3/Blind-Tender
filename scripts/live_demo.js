const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const https = require("https");
const { execSync } = require("child_process");

const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";
const RPC_URL = "https://studio.genlayer.com/api";

globalThis.fetch = (url, opts = {}) =>
  new Promise((resolve, reject) => {
    const u = new URL(url);
    const body = opts.body ? Buffer.from(opts.body) : null;
    const headers = Object.assign({}, opts.headers || {});
    if (!headers["User-Agent"] && !headers["user-agent"]) {
      headers["User-Agent"] = BROWSER_UA;
    }
    if (body && !headers["content-length"] && !headers["Content-Length"]) {
      headers["content-length"] = body.length;
    }
    const req = https.request(
      {
        hostname: u.hostname,
        port: u.port || 443,
        path: u.pathname + u.search,
        method: opts.method || "GET",
        headers,
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const buf = Buffer.concat(chunks);
          const text = buf.toString("utf8");
          const h = new Headers();
          for (const [k, v] of Object.entries(res.headers)) {
            if (Array.isArray(v)) {
              for (const item of v) h.append(k, item);
            } else if (v !== undefined) {
              h.set(k, String(v));
            }
          }
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 300,
            status: res.statusCode,
            statusText: res.statusMessage || "",
            headers: h,
            text: async () => text,
            json: async () => JSON.parse(text),
            arrayBuffer: async () => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength),
          });
        });
      }
    );
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });

const { createClient, createAccount, generatePrivateKey } = require("genlayer-js");
const { studionet } = require("genlayer-js/chains");

const ROOT_DIR = path.resolve(__dirname, "..");
const STATE_FILE = path.join(__dirname, "state.json");
const ROOT_MANIFEST_FILE = path.join(ROOT_DIR, "release_manifest.json");
const ARTIFACT_MANIFEST_FILE = path.join(ROOT_DIR, "artifacts", "release_manifest.json");
const RUNNER = "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6";
const REPO_PREFIX = "https://raw.githubusercontent.com/Kanezar3/Blind-Tender/";

function sha256Hex(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

function computeBidCommitment(tenderId, bidId, bidder, price, contentHash, salt) {
  const preimage = `${tenderId}|${bidId}|${bidder.trim().toLowerCase()}|${price}|${contentHash.trim().toLowerCase()}|${salt}`;
  return crypto.createHash("sha256").update(preimage, "utf8").digest("hex");
}

function normalizeOnChainCode(codeResp) {
  if (typeof codeResp !== "string") return "";
  if (codeResp.startsWith("0x")) {
    return Buffer.from(codeResp.slice(2), "hex").toString("utf8");
  }
  return codeResp;
}

function normalizeReturn(val) {
  if (typeof val === "string") {
    const s = val.trim();
    if (s.startsWith('"') && s.endsWith('"')) {
      try {
        return JSON.parse(s);
      } catch {
        return s.slice(1, -1);
      }
    }
    return s;
  }
  return val;
}

async function rpcCall(method, params) {
  const resp = await fetch(RPC_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const data = await resp.json();
  if (data.error) {
    throw new Error(`RPC ${method} error: ${JSON.stringify(data.error)}`);
  }
  return data.result;
}

async function getBalanceWei(address) {
  const hex = await rpcCall("eth_getBalance", [address, "latest"]);
  return BigInt(hex).toString();
}

function loadState() {
  if (fs.existsSync(STATE_FILE)) {
    try {
      return JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    } catch {
      return {};
    }
  }
  return {};
}

function saveState(patch) {
  const cur = loadState();
  const next = { ...cur, ...patch, updatedAt: new Date().toISOString() };
  fs.writeFileSync(STATE_FILE, JSON.stringify(next, null, 2) + "\n", "utf8");
  return next;
}

async function waitForFinalized(client, hash, label, expectFailure = false) {
  let acceptedSeen = false;
  const observedStatuses = [];
  const maxAttempts = 150;
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const tx = await client.getTransaction({ hash });
      const status = String(tx.statusName || tx.status || "");
      if (status && observedStatuses[observedStatuses.length - 1] !== status) {
        observedStatuses.push(status);
      }
      if (
        ["PENDING", "PROPOSING", "COMMITTING", "REVEALING", "ACCEPTED", "1", "2", "3", "4", "5", "6"].includes(status)
      ) {
        acceptedSeen = true;
      }
      if (status === "FINALIZED" || status === "7") {
        const lr = tx.consensus_data?.leader_receipt?.[0] || {};
        const execResult = lr.execution_result || tx.txExecutionResultName || "";
        const rawPayload = lr.result?.payload?.readable ?? lr.result?.payload ?? "";
        const returnPayload = normalizeReturn(rawPayload);
        const stderr = lr.genvm_result?.stderr || lr.error || "";
        const votes = tx.consensus_data?.votes || {};
        if (!acceptedSeen && votes && Object.keys(votes).length > 0) {
          acceptedSeen = true;
        }
        const resultName = tx.resultName || tx.result_name || String(tx.result || "");
        const contractAddress =
          tx.to_address || tx.contract_address || (tx.data && tx.data.contract_address) || "";

        const isSuccess = ["SUCCESS", "FINISHED_WITH_RETURN"].includes(execResult);
        const isError = ["ERROR", "FINISHED_WITH_ERROR"].includes(execResult);

        console.log(
          `[${label}] tx=${hash} | acceptedSeen=${acceptedSeen} | statuses=${observedStatuses.join("->")} | result=${resultName} | leader_exec=${execResult} | ret=${JSON.stringify(returnPayload)} | err=${stderr || "none"}`
        );

        if (!expectFailure && !isSuccess) {
          throw new Error(
            `[${label}] Expected leader SUCCESS, got ${execResult}: stderr=${stderr} payload=${JSON.stringify(returnPayload)}`
          );
        }
        if (expectFailure && !isError) {
          throw new Error(
            `[${label}] Expected leader ERROR, got ${execResult}: payload=${JSON.stringify(returnPayload)}`
          );
        }

        return {
          step: label,
          tx_hash: hash,
          explorer_url: `https://explorer-studio.genlayer.com/tx/${hash}`,
          accepted_seen: acceptedSeen,
          observed_statuses: observedStatuses,
          final_status: status,
          consensus_result: resultName,
          leader_execution_result: execResult,
          return_or_error: returnPayload || stderr || "",
          stderr: stderr || null,
          votes,
          expected_failure: expectFailure,
          contract_address: contractAddress,
        };
      }
      if (["UNDETERMINED", "CANCELED", "VALIDATORS_TIMEOUT", "LEADER_TIMEOUT"].includes(status)) {
        throw new Error(`[${label}] Terminal non-finalized status ${status} on ${hash}`);
      }
    } catch (err) {
      if (err.message && (err.message.includes("Expected leader") || err.message.includes("Terminal non-finalized"))) {
        throw err;
      }
      console.warn(`  [Poll attempt ${i + 1} warning for ${label}]: ${err.message || err}`);
    }
    await new Promise((r) => setTimeout(r, i < 5 ? 1000 : 3500));
  }
  throw new Error(`[${label}] Timeout waiting for FINALIZED on ${hash}`);
}

async function readWithRetry(client, address, functionName, args = [], retries = 6) {
  let lastErr;
  for (let i = 1; i <= retries; i++) {
    try {
      return await client.readContract({ address, functionName, args });
    } catch (err) {
      lastErr = err;
      console.warn(`  [readContract ${functionName} attempt ${i}]: ${err.message || err}`);
      await new Promise((r) => setTimeout(r, 2500 * i));
    }
  }
  throw lastErr;
}

async function sendAndVerify(
  client,
  contractAddress,
  functionName,
  args,
  label,
  expectFailure = false,
  valueWei = 0n,
  expectedErrSubstr = null,
  txFees = undefined,
  retries = 5
) {
  let hash = null;
  let lastErr = null;
  for (let i = 1; i <= retries; i++) {
    try {
      hash = await client.writeContract({
        address: contractAddress,
        functionName,
        args,
        value: valueWei,
        fees: txFees,
      });
      break;
    } catch (err) {
      lastErr = err;
      console.warn(`  [writeContract ${label} attempt ${i}]: ${err.message || err}`);
      if (i < retries) await new Promise((r) => setTimeout(r, 3000 * i));
    }
  }
  if (!hash) {
    throw new Error(`Failed to send ${label}: ${lastErr?.message || lastErr}`);
  }
  const rec = await waitForFinalized(client, hash, label, expectFailure);
  if (expectedErrSubstr && !String(rec.return_or_error).includes(expectedErrSubstr)) {
    throw new Error(`[${label}] Expected '${expectedErrSubstr}' in error, got: ${rec.return_or_error}`);
  }
  return rec;
}

async function main() {
  const commitSha = execSync("git rev-parse HEAD", { cwd: ROOT_DIR }).toString("utf8").trim();
  const blobSha = execSync("git rev-parse HEAD:contracts/blind_tender.py", { cwd: ROOT_DIR })
    .toString("utf8")
    .trim();

  const contractPath = path.join(ROOT_DIR, "contracts", "blind_tender.py");
  const contractBytes = fs.readFileSync(contractPath);
  const contractCode = contractBytes.toString("utf8");
  const contractSha256 = sha256Hex(contractBytes);

  const evidenceFiles = {
    bid_a: "evidence/bid_a_lowball_no_cert.txt",
    bid_b: "evidence/bid_b_compliant_winner.txt",
    bid_c: "evidence/bid_c_compliant_runnerup.txt",
  };

  const evidenceMeta = {};
  for (const [k, rel] of Object.entries(evidenceFiles)) {
    const raw = fs.readFileSync(path.join(ROOT_DIR, rel));
    evidenceMeta[k] = {
      path: rel,
      bytes: raw.length,
      sha256: sha256Hex(raw),
      raw_url: `${REPO_PREFIX}${commitSha}/${rel}`,
    };
  }

  const ROLE_NAMES = ["buyer", "vendorA", "vendorB", "vendorC", "auditor", "attacker"];
  let state = loadState();
  if (!state.keys) {
    const keys = {};
    for (const role of ROLE_NAMES) {
      const pk = generatePrivateKey();
      const acc = createAccount(pk);
      keys[role] = { privateKey: pk, address: acc.address };
    }
    state = saveState({ keys });
  }

  const roles = {};
  for (const role of ROLE_NAMES) {
    const account = createAccount(state.keys[role].privateKey);
    const client = createClient({ chain: studionet, account });
    roles[role] = { address: account.address, account, client };
  }

  if (!state.funded) {
    const funding = {};
    for (const role of ROLE_NAMES) {
      const addr = roles[role].address;
      await rpcCall("sim_fundAccount", [addr, 10000000000000000]);
      funding[role] = await getBalanceWei(addr);
      console.log(`Funded ${role} (${addr}) -> balance=${funding[role]} wei`);
    }
    state = saveState({ funded: true, initialBalances: funding });
  }

  const feeConfig = await rpcCall("sim_getFeeConfig", []);
  const txFees = feeConfig?.enabled ? feeConfig.defaultFees : undefined;
  console.log("StudioNet fees enabled:", Boolean(feeConfig?.enabled), "feeValue:", txFees?.feeValue || "0");

  console.log("Deploy commit:", commitSha);
  console.log("Contract blob:", blobSha);
  console.log("Contract sha256:", contractSha256);

  if (!state.contractAddress) {
    const deployHash = await roles.buyer.client.deployContract({
      code: contractCode,
      args: [],
      fees: txFees,
    });
    const deployTx = await waitForFinalized(roles.buyer.client, deployHash, "DEPLOY", false);
    const contractAddress = deployTx.contract_address;
    if (!contractAddress) {
      throw new Error("Could not extract deployed contract address from receipt");
    }
    const onChainCodeRaw = await roles.buyer.client.getContractCode(contractAddress);
    const onChainCode = normalizeOnChainCode(onChainCodeRaw);
    const onChainSha256 = sha256Hex(Buffer.from(onChainCode, "utf8"));
    const byteIdentical = onChainSha256 === contractSha256;
    console.log("Deployed BlindTender v2 contract at:", contractAddress);
    console.log("Deployed source byte-identical to local/Git file:", byteIdentical);
    if (!byteIdentical) {
      throw new Error(`Deployed source mismatch: local=${contractSha256} onchain=${onChainSha256}`);
    }
    state = saveState({
      contractAddress,
      deployTx,
      onChainSha256,
      byteIdentical,
      txs: state.txs || [],
      readbacks: state.readbacks || {},
    });
  }

  const contractAddress = state.contractAddress;
  const txs = state.txs || [];
  const readbacks = state.readbacks || {};

  function writeManifests() {
    const probePath = path.join(ROOT_DIR, "artifacts", "runner_probe.json");
    const probeData = fs.existsSync(probePath) ? JSON.parse(fs.readFileSync(probePath, "utf8")) : null;
    const manifest = {
      contract_name: "BlindTender",
      display_name: "Blind Tender",
      network: "studionet",
      chain_id: 61999,
      rpc_url: RPC_URL,
      contract_address: contractAddress,
      explorer_contract_url: `https://explorer-studio.genlayer.com/address/${contractAddress}`,
      repo_url: "https://github.com/Kanezar3/Blind-Tender",
      commit_sha: commitSha,
      contract_blob_sha: blobSha,
      contract_sha256: contractSha256,
      on_chain_code_sha256: state.onChainSha256,
      deployed_source_byte_identical: state.byteIdentical,
      pinned_contract_url: `https://github.com/Kanezar3/Blind-Tender/blob/${commitSha}/contracts/blind_tender.py`,
      pinned_contract_raw_url: `${REPO_PREFIX}${commitSha}/contracts/blind_tender.py`,
      runner: RUNNER,
      runner_probe: probeData,
      toolchain: {
        node: process.version,
        "genlayer-js": "2.0.0",
      },
      constructor_args: [],
      deploy_tx: state.deployTx,
      accounts: {
        buyer: roles.buyer.address,
        vendorA: roles.vendorA.address,
        vendorB: roles.vendorB.address,
        vendorC: roles.vendorC.address,
        auditor: roles.auditor.address,
        attacker: roles.attacker.address,
      },
      evidence_fixtures: evidenceMeta,
      transactions: txs,
      readbacks,
    };
    fs.mkdirSync(path.dirname(ARTIFACT_MANIFEST_FILE), { recursive: true });
    const jsonOut = JSON.stringify(manifest, null, 2) + "\n";
    fs.writeFileSync(ROOT_MANIFEST_FILE, jsonOut, "utf8");
    fs.writeFileSync(ARTIFACT_MANIFEST_FILE, jsonOut, "utf8");
  }

  async function runStep(stepName, fn) {
    if (txs.find((t) => t.step === stepName)) {
      console.log(`[SKIP] ${stepName} already recorded.`);
      return;
    }
    const txRec = await fn();
    txs.push(txRec);
    saveState({ txs, readbacks });
    writeManifests();
  }

  const tx = (client, fn, args, label, expectFailure = false, valueWei = 0n, expectedErrSubstr = null) =>
    sendAndVerify(client, contractAddress, fn, args, label, expectFailure, valueWei, expectedErrSubstr, txFees);

  const MANDATORY_REQS =
    "Utility Grid-Scale BESS Procurement: usable capacity >= 100 MWh, active UL-9540A thermal-runaway safety certification, and >= 6000 warranted full cycles.";
  const TENDER_ID = "bess-live-v2";

  // X1: Open tender with zero escrow balance -> INSUFFICIENT_ESCROW_BALANCE, balance stays 0
  await runStep("X1_INSUFFICIENT_ESCROW_BALANCE", async () => {
    const rec = await tx(
      roles.buyer.client,
      "open_tender",
      [TENDER_ID, 1000, 3, 3600, 3600, MANDATORY_REQS, REPO_PREFIX],
      "X1_INSUFFICIENT_ESCROW_BALANCE",
      true,
      0n,
      "INSUFFICIENT_ESCROW_BALANCE"
    );
    readbacks.after_X1_accounting = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_accounting_state", [])
    );
    if (readbacks.after_X1_accounting.contract_balance !== 0) {
      throw new Error("Non-zero contract balance after X1");
    }
    return rec;
  });

  // S0: Buyer deposits 1000 wei; Vendors A, B, C each deposit 50 wei
  await runStep("S0_DEPOSIT_BUYER", async () =>
    tx(roles.buyer.client, "deposit_escrow", [], "S0_DEPOSIT_BUYER", false, 1000n)
  );
  await runStep("S0A_DEPOSIT_VENDOR_A", async () =>
    tx(roles.vendorA.client, "deposit_escrow", [], "S0A_DEPOSIT_VENDOR_A", false, 50n)
  );
  await runStep("S0B_DEPOSIT_VENDOR_B", async () =>
    tx(roles.vendorB.client, "deposit_escrow", [], "S0B_DEPOSIT_VENDOR_B", false, 50n)
  );
  await runStep("S0C_DEPOSIT_VENDOR_C", async () => {
    const rec = await tx(
      roles.vendorC.client,
      "deposit_escrow",
      [],
      "S0C_DEPOSIT_VENDOR_C",
      false,
      50n
    );
    readbacks.after_S0_deposits_accounting = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_accounting_state", [])
    );
    return rec;
  });

  // S1: Buyer opens sealed-bid Vickrey tender (reserve=1000, min_bond=50, max_bidders=3)
  await runStep("S1_OPEN_TENDER", async () => {
    const rec = await tx(
      roles.buyer.client,
      "open_tender",
      [TENDER_ID, 1000, 3, 3600, 3600, MANDATORY_REQS, REPO_PREFIX],
      "S1_OPEN_TENDER",
      false
    );
    readbacks.after_S1_tender = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_tender", [TENDER_ID])
    );
    return rec;
  });

  const hA = computeBidCommitment(TENDER_ID, "bid-a", roles.vendorA.address, 417, evidenceMeta.bid_a.sha256, "salt-live-a");
  const hB = computeBidCommitment(TENDER_ID, "bid-b", roles.vendorB.address, 673, evidenceMeta.bid_b.sha256, "salt-live-b");
  const hC = computeBidCommitment(TENDER_ID, "bid-c", roles.vendorC.address, 829, evidenceMeta.bid_c.sha256, "salt-live-c");

  // X2: Insufficient bid bond (49 < 50) rejected
  await runStep("X2_INSUFFICIENT_BID_BOND", async () =>
    tx(
      roles.vendorA.client,
      "commit_bid",
      [TENDER_ID, "bid-a", 49, hA],
      "X2_INSUFFICIENT_BID_BOND",
      true,
      0n,
      "INSUFFICIENT_BID_BOND"
    )
  );

  // X3: Buyer cannot bid on own tender
  await runStep("X3_BUYER_CANNOT_BID", async () =>
    tx(
      roles.buyer.client,
      "commit_bid",
      [TENDER_ID, "bid-self", 50, hA],
      "X3_BUYER_CANNOT_BID",
      true,
      0n,
      "BUYER_CANNOT_BID"
    )
  );

  // S2A/S2B/S2C: Three vendors commit sealed (price + dossier_hash + salt) hashes
  await runStep("S2A_COMMIT_BID_A", async () =>
    tx(roles.vendorA.client, "commit_bid", [TENDER_ID, "bid-a", 50, hA], "S2A_COMMIT_BID_A", false)
  );
  await runStep("S2B_COMMIT_BID_B", async () =>
    tx(roles.vendorB.client, "commit_bid", [TENDER_ID, "bid-b", 50, hB], "S2B_COMMIT_BID_B", false)
  );
  await runStep("S2C_COMMIT_BID_C", async () => {
    const rec = await tx(
      roles.vendorC.client,
      "commit_bid",
      [TENDER_ID, "bid-c", 50, hC],
      "S2C_COMMIT_BID_C",
      false
    );
    readbacks.after_S2C_tender = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_tender", [TENDER_ID])
    );
    if (readbacks.after_S2C_tender.status !== "REVEAL_OPEN") {
      throw new Error("Expected REVEAL_OPEN after 3rd commit");
    }
    return rec;
  });

  // X4: Unauthorized attacker trying to reveal VendorB's bid -> UNAUTHORIZED_CALLER
  await runStep("X4_UNAUTHORIZED_REVEAL", async () =>
    tx(
      roles.attacker.client,
      "reveal_bid",
      [TENDER_ID, "bid-b", 673, "salt-live-b", evidenceMeta.bid_b.raw_url, evidenceMeta.bid_b.sha256],
      "X4_UNAUTHORIZED_REVEAL",
      true,
      0n,
      "UNAUTHORIZED_CALLER"
    )
  );

  // X5: Tampered reveal price -> COMMITMENT_MISMATCH
  await runStep("X5_COMMITMENT_MISMATCH_PRICE", async () =>
    tx(
      roles.vendorB.client,
      "reveal_bid",
      [TENDER_ID, "bid-b", 600, "salt-live-b", evidenceMeta.bid_b.raw_url, evidenceMeta.bid_b.sha256],
      "X5_COMMITMENT_MISMATCH_PRICE",
      true,
      0n,
      "COMMITMENT_MISMATCH"
    )
  );

  // X6: Post-reveal dossier swap (swapping in non-compliant dossier A) -> COMMITMENT_MISMATCH
  await runStep("X6_COMMITMENT_MISMATCH_DOSSIER", async () =>
    tx(
      roles.vendorB.client,
      "reveal_bid",
      [TENDER_ID, "bid-b", 673, "salt-live-b", evidenceMeta.bid_a.raw_url, evidenceMeta.bid_a.sha256],
      "X6_COMMITMENT_MISMATCH_DOSSIER",
      true,
      0n,
      "COMMITMENT_MISMATCH"
    )
  );

  // X7: Mutable /main/ branch URL -> INVALID_EVIDENCE_URL
  await runStep("X7_UNPINNED_MAIN_URL", async () =>
    tx(
      roles.vendorB.client,
      "reveal_bid",
      [TENDER_ID, "bid-b", 673, "salt-live-b", `${REPO_PREFIX}main/evidence/bid_b_compliant_winner.txt`, evidenceMeta.bid_b.sha256],
      "X7_UNPINNED_MAIN_URL",
      true,
      0n,
      "INVALID_EVIDENCE_URL"
    )
  );

  // X8: Foreign repository URL -> REPO_PREFIX_MISMATCH
  await runStep("X8_FOREIGN_REPO_URL", async () =>
    tx(
      roles.vendorB.client,
      "reveal_bid",
      [
        TENDER_ID,
        "bid-b",
        673,
        "salt-live-b",
        `https://raw.githubusercontent.com/attacker/evil/${commitSha}/evidence/bid_b_compliant_winner.txt`,
        evidenceMeta.bid_b.sha256,
      ],
      "X8_FOREIGN_REPO_URL",
      true,
      0n,
      "REPO_PREFIX_MISMATCH"
    )
  );

  // S3A/S3B/S3C: Verified sealed-bid reveals with commit-pinned unstructured prose dossiers
  await runStep("S3A_REVEAL_BID_A", async () =>
    tx(
      roles.vendorA.client,
      "reveal_bid",
      [TENDER_ID, "bid-a", 417, "salt-live-a", evidenceMeta.bid_a.raw_url, evidenceMeta.bid_a.sha256],
      "S3A_REVEAL_BID_A",
      false
    )
  );
  await runStep("S3B_REVEAL_BID_B", async () =>
    tx(
      roles.vendorB.client,
      "reveal_bid",
      [TENDER_ID, "bid-b", 673, "salt-live-b", evidenceMeta.bid_b.raw_url, evidenceMeta.bid_b.sha256],
      "S3B_REVEAL_BID_B",
      false
    )
  );
  await runStep("S3C_REVEAL_BID_C", async () => {
    const rec = await tx(
      roles.vendorC.client,
      "reveal_bid",
      [TENDER_ID, "bid-c", 829, "salt-live-c", evidenceMeta.bid_c.raw_url, evidenceMeta.bid_c.sha256],
      "S3C_REVEAL_BID_C",
      false
    );
    readbacks.after_S3C_tender = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_tender", [TENDER_ID])
    );
    return rec;
  });

  // X9: Premature award_tender before price-blind evaluations finish -> UNEVALUATED_BID_EXISTS
  await runStep("X9_PREMATURE_AWARD", async () =>
    tx(
      roles.attacker.client,
      "award_tender",
      [TENDER_ID],
      "X9_PREMATURE_AWARD",
      true,
      0n,
      "UNEVALUATED_BID_EXISTS"
    )
  );

  // S4A/S4B/S4C: Live Price-Blind Consensus Eligibility Evaluation over unstructured prose
  await runStep("S4A_EVAL_BID_A", async () => {
    const rec = await tx(roles.auditor.client, "evaluate_bid", [TENDER_ID, "bid-a"], "S4A_EVAL_BID_A", false);
    readbacks.after_S4A_bid_a = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_bid", [TENDER_ID, "bid-a"])
    );
    if (
      readbacks.after_S4A_bid_a.eval_verdict !== "DISQUALIFIED" ||
      readbacks.after_S4A_bid_a.eval_reason !== "COMPLIANCE_CERT_MISSING"
    ) {
      throw new Error("Unexpected bid-a verdict: " + JSON.stringify(readbacks.after_S4A_bid_a));
    }
    return rec;
  });

  await runStep("S4B_EVAL_BID_B", async () => {
    const rec = await tx(roles.auditor.client, "evaluate_bid", [TENDER_ID, "bid-b"], "S4B_EVAL_BID_B", false);
    readbacks.after_S4B_bid_b = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_bid", [TENDER_ID, "bid-b"])
    );
    if (
      readbacks.after_S4B_bid_b.eval_verdict !== "ELIGIBLE" ||
      readbacks.after_S4B_bid_b.eval_reason !== "ALL_REQUIREMENTS_MET"
    ) {
      throw new Error("Unexpected bid-b verdict: " + JSON.stringify(readbacks.after_S4B_bid_b));
    }
    return rec;
  });

  await runStep("S4C_EVAL_BID_C", async () => {
    const rec = await tx(roles.auditor.client, "evaluate_bid", [TENDER_ID, "bid-c"], "S4C_EVAL_BID_C", false);
    readbacks.after_S4C_bid_c = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_bid", [TENDER_ID, "bid-c"])
    );
    if (
      readbacks.after_S4C_bid_c.eval_verdict !== "ELIGIBLE" ||
      readbacks.after_S4C_bid_c.eval_reason !== "ALL_REQUIREMENTS_MET"
    ) {
      throw new Error("Unexpected bid-c verdict: " + JSON.stringify(readbacks.after_S4C_bid_c));
    }
    return rec;
  });

  // X10: Replay evaluation on already-concluded bid blocked -> BID_ALREADY_CONCLUDED
  await runStep("X10_REPLAY_CONCLUDED_EVAL", async () =>
    tx(
      roles.attacker.client,
      "evaluate_bid",
      [TENDER_ID, "bid-a"],
      "X10_REPLAY_CONCLUDED_EVAL",
      true,
      0n,
      "BID_ALREADY_CONCLUDED"
    )
  );

  // S5: Deterministic Vickrey Second-Price Award (bid-b wins at 829; lowball 417 ignored)
  await runStep("S5_AWARD_TENDER", async () => {
    const rec = await tx(roles.auditor.client, "award_tender", [TENDER_ID], "S5_AWARD_TENDER", false);
    readbacks.after_S5_tender = JSON.parse(
      await readWithRetry(roles.buyer.client, contractAddress, "get_tender", [TENDER_ID])
    );
    if (
      readbacks.after_S5_tender.status !== "AWARDED" ||
      readbacks.after_S5_tender.winner_bid_id !== "bid-b" ||
      readbacks.after_S5_tender.clearing_price !== 829
    ) {
      throw new Error("Unexpected award result: " + JSON.stringify(readbacks.after_S5_tender));
    }
    return rec;
  });

  // S6A-S6D: Pull native wei payouts via _EoaRecipient.emit_transfer
  for (const [stepName, roleKey, expectedPayout] of [
    ["S6A_WITHDRAW_VENDOR_B", "vendorB", 879],
    ["S6B_WITHDRAW_BUYER", "buyer", 171],
    ["S6C_WITHDRAW_VENDOR_A", "vendorA", 50],
    ["S6D_WITHDRAW_VENDOR_C", "vendorC", 50],
  ]) {
    await runStep(stepName, async () => {
      const contractBefore = await getBalanceWei(contractAddress);
      const balBefore = await getBalanceWei(roles[roleKey].address);
      const rec = await tx(roles[roleKey].client, "withdraw_payout", [], stepName, false);
      const contractAfter = await getBalanceWei(contractAddress);
      const balAfter = await getBalanceWei(roles[roleKey].address);
      const contractDelta = (BigInt(contractBefore) - BigInt(contractAfter)).toString();
      readbacks[`after_${stepName}_transfer`] = {
        expected_payout_wei: String(expectedPayout),
        contract_balance_before_wei: contractBefore,
        contract_balance_after_wei: contractAfter,
        contract_disbursed_wei: contractDelta,
        eoa_before_wei: balBefore,
        eoa_after_wei: balAfter,
      };
      return rec;
    });
  }

  // X11: Attacker with 0 pending payout cannot withdraw
  await runStep("X11_NO_PENDING_PAYOUT", async () =>
    tx(roles.attacker.client, "withdraw_payout", [], "X11_NO_PENDING_PAYOUT", true, 0n, "NO_PENDING_PAYOUT")
  );

  // Final accounting readback
  readbacks.final_accounting = JSON.parse(
    await readWithRetry(roles.buyer.client, contractAddress, "get_accounting_state", [])
  );
  saveState({ txs, readbacks });
  writeManifests();

  console.log("\n=== FINAL ACCOUNTING STATE ===");
  console.log(JSON.stringify(readbacks.final_accounting, null, 2));
  if (
    readbacks.final_accounting.contract_balance !== 0 ||
    readbacks.final_accounting.total_deposited !== 1150 ||
    readbacks.final_accounting.total_withdrawn !== 1150
  ) {
    throw new Error("Final accounting conservation failed: " + JSON.stringify(readbacks.final_accounting));
  }
  console.log("ALL LIVE STUDIONET V2 STEPS VERIFIED & SAVED TO release_manifest.json!");
}

main().catch((err) => {
  console.error("LIVE DEMO FAILED:", err);
  process.exit(1);
});
