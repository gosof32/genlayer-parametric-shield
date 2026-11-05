// Reproducible end-to-end lifecycle of Parametric Shield on GenLayer Studionet.
//
//   npm install && npm run lifecycle:studionet
//
// Deploys contracts/parametric_shield.py and drives two real covers through their
// whole life against live Open-Meteo weather data for Pekanbaru, Riau (October 2026):
//   Cover 1: highest daily rainfall >= 15 mm  -> expected TRIGGERED
//   Cover 2: monthly rainfall total  >= 300 mm -> expected NOT_TRIGGERED
// Every transaction hash and its finalized status is written to
// deployments/studionet.json. Throwaway keys are used (funded with
// sim_fundAccount), so no real funds are involved.

import fs from "node:fs";
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";

const WAIT = { status: "FINALIZED", retries: 300, interval: 3000 };
const client = createClient({ chain: studionet });
const underwriter = createAccount(process.env.DEPLOYER_PRIVATE_KEY || generatePrivateKey());
const farmer = createAccount(generatePrivateKey());
const outsider = createAccount(generatePrivateKey());

const UNIT = 1_000_000n; // contract amounts are plain integers; small units keep funding simple
const log = [];
const json = (v) => JSON.parse(JSON.stringify(v, (_, x) => (typeof x === "bigint" ? x.toString() : x instanceof Map ? Object.fromEntries(x) : x)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function describe(receipt) {
  const r = json(receipt);
  const leader = r.consensus_data?.leader_receipt?.[0] ?? r.consensus_data?.leader_receipt ?? {};
  const execution = leader.execution_result ?? r.txExecutionResultName ?? "UNKNOWN";
  const status = r.status_name ?? r.statusName ?? r.status;
  const consensus = r.result_name ?? r.resultName ?? r.result;
  let returnValue = leader.result?.payload?.readable ?? leader.result?.payload ?? null;
  if (returnValue && typeof returnValue !== "string") returnValue = JSON.stringify(returnValue);
  return { status, consensus, execution, ok: execution === "SUCCESS", returnValue };
}

async function step(label, account, fn, expectOk = true) {
  process.stdout.write(`- ${label} ... `);
  const hash = await fn(account);
  const receipt = await client.waitForTransactionReceipt({ hash, ...WAIT });
  const d = describe(receipt);
  log.push({ label, hash, from: account.address, ...d, expected_ok: expectOk });
  console.log(`${d.status}/${d.execution}${d.returnValue ? " " + String(d.returnValue).slice(0, 160) : ""}`);
  if (d.ok !== expectOk) throw new Error(`Unexpected outcome for "${label}": ${JSON.stringify(d)}`);
  return { hash, receipt, d };
}

const write = (address, functionName, args, value = 0n) => (account) =>
  client.writeContract({ account, address, functionName, args, value });
const read = (address, functionName, args) => client.readContract({ address, functionName, args });

for (const a of [underwriter, farmer, outsider]) {
  await client.request({ method: "sim_fundAccount", params: [a.address, 1_000_000_000_000] });
}

const code = fs.readFileSync(new URL("../contracts/parametric_shield.py", import.meta.url), "utf8");
const deploy = await step("deploy contract", underwriter, (account) => client.deployContract({ account, code, args: [] }));
const address = deploy.receipt.data?.contract_address ?? deploy.receipt.recipient ?? deploy.receipt.to_address;
console.log(`  contract: ${address}`);

const Q = "latitude=0.51&longitude=101.45&start_date=2026-10-01&end_date=2026-10-31&daily=precipitation_sum&timezone=UTC";
const sources = (reducer) => JSON.stringify([
  { url: `https://archive-api.open-meteo.com/v1/archive?${Q}`, path: "daily.precipitation_sum", reducer },
  { url: `https://archive-api.open-meteo.com/v1/archive?${Q}&models=era5`, path: "daily.precipitation_sum", reducer },
  { url: `https://historical-forecast-api.open-meteo.com/v1/forecast?${Q}`, path: "daily.precipitation_sum", reducer },
]);

// The coverage window opens a few minutes from now so cover can be sold first.
const start = new Date(Date.now() + 4 * 60_000);
const end = new Date(start.getTime() + 60_000);
const iso = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");

const covers = [
  ["Pekanbaru heavy-rain day, Oct 2026", "highest single-day rainfall in Pekanbaru, in millimetres", "15", sources("max")],
  ["Pekanbaru wet month, Oct 2026", "total monthly rainfall in Pekanbaru, in millimetres", "300", sources("sum")],
];
for (const [title, metric, threshold, src] of covers) {
  await step(`create_cover "${title}" (>= ${threshold})`, underwriter, write(address, "create_cover", [
    title, metric, "gte", threshold, src, 2, 500, "1", iso(start), iso(end), 1, 800,
  ], 1000n * UNIT));
}

for (const id of [1, 2]) {
  const premium = await read(address, "quote_premium", [id, 100n * UNIT]);
  await step(`buy_cover #${id} (100 units, premium ${premium})`, farmer, write(address, "buy_cover", [id, 100n * UNIT], BigInt(premium)));
}
await step("buy_cover #1 with wrong premium (must be rejected)", outsider, write(address, "buy_cover", [1, 100n * UNIT], 1n), false);

const waitMs = end.getTime() - Date.now() + 15_000;
if (waitMs > 0) {
  console.log(`  waiting ${Math.ceil(waitMs / 1000)}s for the coverage window to close...`);
  await sleep(waitMs);
}

await step("resolve #1 (validators read 3 live Open-Meteo sources)", outsider, write(address, "resolve", [1]));
await step("resolve #2 (validators read 3 live Open-Meteo sources)", outsider, write(address, "resolve", [2]));
await step("resolve #1 again (must be rejected: already resolved)", outsider, write(address, "resolve", [1]), false);
await step("claim #1 by farmer (triggered: pays coverage)", farmer, write(address, "claim", [1]));
await step("claim #2 by farmer (must be rejected: did not trigger)", farmer, write(address, "claim", [2]), false);
await step("claim #1 by outsider (must be rejected: no position)", outsider, write(address, "claim", [1]), false);
await step("withdraw_underwriter #1 (capital + premiums - payout)", underwriter, write(address, "withdraw_underwriter", [1]));
await step("withdraw_underwriter #2 (capital + premiums)", underwriter, write(address, "withdraw_underwriter", [2]));

const final_state = json(await read(address, "list_covers", []));
fs.mkdirSync(new URL("../deployments/", import.meta.url), { recursive: true });
fs.writeFileSync(
  new URL("../deployments/studionet.json", import.meta.url),
  JSON.stringify(json({ network: "studionet", rpc: "https://studio.genlayer.com/api", contract: address,
    generated_at: new Date().toISOString(), transactions: log, final_state }), null, 2) + "\n",
);
console.log("\nWrote deployments/studionet.json");
