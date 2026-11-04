const express = require("express");
const cors = require("cors");
const { randomBytes } = require("crypto");
const { LambdaClient, InvokeCommand } = require("@aws-sdk/client-lambda");
const {
  CognitoIdentityClient,
  GetIdCommand,
  GetCredentialsForIdentityCommand,
} = require("@aws-sdk/client-cognito-identity");

const WEBHOOK_URL =
  process.env.DISCORD_WEBHOOK_URL ||
  "REDACTED_DISCORD_WEBHOOK";

const MAP_WEBHOOK_URL =
  process.env.MAP_DISCORD_WEBHOOK_URL ||
  "REDACTED_DISCORD_WEBHOOK";

const app = express();

// Player APK downloads (self-hosted). The Cloudflare Tunnel routes
// api.your-domain.example/download/* here: Node streams big files with proper
// backpressure (+ Range support), whereas the Python game server buffered a
// 385 MB APK into RAM/swap per download and got OOM-killed under 18 downloads.
// Files come from ~/yeeps-public (env PUBLIC_DOWNLOAD_DIR); plain .apk names only.
{
  const path = require("path"), fs = require("fs"), os = require("os");
  const PUBLIC_DIR = process.env.PUBLIC_DOWNLOAD_DIR || path.join(os.homedir(), "yeeps-public");
  app.get("/download/:name", (req, res) => {
    const name = req.params.name;
    if (!/^[A-Za-z0-9._-]+\.apk$/i.test(name) || name.startsWith(".")) return res.status(404).end();
    const file = path.join(PUBLIC_DIR, name);
    fs.stat(file, (err, st) => {
      if (err || !st.isFile()) return res.status(404).end();
      res.set("Cache-Control", "public, max-age=86400");
      res.attachment(name);
      res.sendFile(file, { headers: { "Content-Type": "application/vnd.android.package-archive" } });
    });
  });

  // Floating-video chunks (editor Announce tab). vr-server writes them once as
  // base64 text to ~/yeeps-public/video/<id>/seg<n>.b64 + audio.b64; the tunnel
  // routes api.your-domain.example/client/video/* here so ~20 players pulling them at
  // once never touch the game server's memory (that froze it on 2026-10-08).
  // URLs end in .bin (v.bin?s=seg<n>.b64, a.bin?v=<ts>) because Cloudflare only
  // edge-caches known extensions: each chunk then leaves this PC about once per
  // Cloudflare location instead of once per player (~90 MB per player otherwise).
  const VIDEO_DIR = process.env.PUBLIC_VIDEO_DIR || path.join(PUBLIC_DIR, "video");
  app.get("/client/video/:vid/:file", (req, res) => {
    const { vid } = req.params;
    let file = req.params.file;
    if (file === "v.bin") file = String(req.query.s || "");
    else if (file === "a.bin") file = "audio.b64";
    if (!/^[0-9a-f]{6,32}$/.test(vid) || !/^(seg\d{1,4}|audio)\.b64$/.test(file)) return res.status(404).end();
    const p = path.join(VIDEO_DIR, vid, file);
    fs.stat(p, (err, st) => {
      if (err || !st.isFile()) return res.status(404).end();
      res.set("Cache-Control", "public, max-age=86400");
      res.sendFile(p, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    });
  });
}

app.use(express.json());
app.use(cors());

// Safety net: never let a stray async error hard-crash the whole server
// (this is what was causing the 502 + restart loop on Render).
process.on("unhandledRejection", (reason) => {
  console.error("[-] Unhandled promise rejection (server kept alive):", reason && reason.message ? reason.message : reason);
});
process.on("uncaughtException", (err) => {
  console.error("[-] Uncaught exception (server kept alive):", err && err.message ? err.message : err);
});

const ENABLE_YEEPS = true;
const ENABLE_BUY = true;
const ENABLE_AWS = true;
const ENABLE_FAV = true;
const ENABLE_MAP = true;
const ENABLE_WEBSITE = true;
const ENABLE_LOOKUP = true;
const ENABLE_ITEMS = true;
const ENABLE_ROOMS = true;

// Community/Private World base keys that are off-limits through this relay,
// regardless of who's asking or what oculusID/mobileCode they supply.
// Checked against the "c_<NAME>"/"p_o_<id>" base extracted from any
// roomKey/worldKey, so "c_YWDS_sandbox", "c_YWDS_room_n2", etc. all match.
const BLOCKED_WORLDS = new Set(["C_YWDS"]);

function baseWorldKey(key) {
  const m = /^(c_[A-Za-z0-9]+|p_o_[A-Za-z0-9]+)/i.exec(key || "");
  return m ? m[1].toUpperCase() : "";
}

function isBlockedWorld(key) {
  return BLOCKED_WORLDS.has(baseWorldKey(key));
}

const BOT_OCULUS_ID   = process.env.BOT_OCULUS_ID || "";
const BOT_MOBILE_CODE = process.env.BOT_MOBILE_CODE || "";

const BASE = "https://eb1v6k1091.execute-api.us-west-1.amazonaws.com";
const LINK_URL = BASE + "/mobileLinkAccount";
const LOGIN_URL = BASE + "/mobileLogIntoAccount";

const HEADERS = {
  Accept: "*/*",
  "Content-Type": "application/json",
  "Accept-Encoding": "gzip, deflate, br",
  "X-Unity-Version": "6000.1.17f1",
  "User-Agent": "Yeeps/0 CFNetwork/3860.400.51 Darwin/25.3.0",
  "Accept-Language": "en-US,en;q=0.9",
};

const YEEPS_PASSWORD = process.env.YEEPS_PASSWORD || "";
const TRUE_VERSION = "2.28.1";

const PROPERTIES = [
  "activeCosmetics",
  "analyticEventKeys",
  "anniversaryPuzzle_isComplete",
  "areMobilePurchasesBlocked",
  "backendVersion",
  "banCategory",
  "banCount",
  "banExpireTimestamp",
  "banReason",
  "banWarnCounter",
  "beanPurchasedCardCount",
  "beans",
  "beans_total",
  "contentCreatorCompletedChallenges",
  "creatorCodeLastRedeemedTime",
  "creatorCodesNumRedeemedThisWeek",
  "crossover_GOG_accountID",
  "currency",
  "currency_cwPayout",
  "cw_admin",
  "cw_favorites",
  "cw_favoritesVersion",
  "cw_freeFuelHistory",
  "cw_nextCanUseFreeFuelTimestamp",
  "cw_staff",
  "cw_vip",
  "cw_vipKeys",
  "dataVersion",
  "displayName",
  "displayName_searchable",
  "eyeColor",
  "firstLogin",
  "hasCreatorPack",
  "hasLinkedMobile",
  "hasPendingMobileLogin",
  "hasPendingWarning",
  "hasUnlockedPrivateRooms",
  "hoursSpent",
  "isMutebanned",
  "isPermabanned",
  "lastCardBeanPurchaseTime",
  "lastCardChallengeRedeemedTime",
  "lastChallengeRedeemedTime_easy",
  "lastChallengeRedeemedTime_hard",
  "lastChallengeRedeemedTime_login",
  "lastLogin",
  "lastLoginStatus",
  "loginStreakExpireTimestamp",
  "loginStreakFreezeHistory",
  "loginStreakFreezeTimestamp",
  "loginStreakLength",
  "loginStreakLongestLastUpdatedTimestamp",
  "loginStreakLongestLength",
  "loginStreakLongestPartition",
  "loginStreakPartition",
  "longestBanDuration",
  "longestBanDuration_prev",
  "lootQuotaProgress_lootQuotas_easy",
  "lootQuotaProgress_lootQuotas_hard",
  "lootQuotaProgress_lootQuotas_medium",
  "mobileCode",
  "mobileLastLogin",
  "mobileLinkFailCount",
  "mobileLoginFailCount",
  "mobileLoginFailTimestamp",
  "mobileNextCanLinkTimestamp",
  "mobileNextCanLoginTimestamp",
  "mobileSessionCount",
  "oculusID",
  "oculusIDFullHistory",
  "oculusIDHistory",
  "oculusID_searchable",
  "oculusNumber",
  "ownedBundles",
  "ownedPatterns",
  "ownedPatterns_favorite",
  "ownedPatterns_hidden",
  "pendingBanDuration",
  "pendingMessages",
  "pets",
  "pets_coinEvents",
  "promoCodeFailCount",
  "pv_coinsSpent_payout",
  "pv_coinsSpent_total",
  "pv_initialized",
  "redeemedCardChallengeKeys",
  "redeemedPromoCodes",
  "redeemedStashIDs",
  "remainingBanHours",
  "reportCount",
  "roleGivenTimestamps",
  "roleKeys",
  "savedOutfits",
  "seenMessages",
  "serverTime",
  "sessionCount",
  "shouldForceRefresh",
  "skinColor",
  "spawnRoomKey",
  "toymakerRequest",
  "warnCategories",
  "warnCount",
  "warnReason",
];

// Only returned from /data when the caller passes secret "<FULL_SECRET>" -- these
// are credential/session material (encrypted login blobs, session/editor
// tokens, nonces) or internal anti-cheat plumbing, not regular player stats.
const SECRET_PROPERTIES = [
  "applicationVersion",
  "achievementsInitDataVersion",
  "banHash",
  "cw_staffEditorSessionID",
  "e_accessKey",
  "e_secretKey",
  "encryptedMobileCode",
  "encryptedMobileIDs",
  "encryptedMobilePassword",
  "encryptedMobileSessionID",
  "encryptedPublicSessionID",
  "encryptedQuestSessionID",
  "gameSessionID",
  "hash",
  "hashedChallengeNonce",
  "hashProperties",
  "lastEditorSessionID",
  "lastValidatedEncryptedQuestSessionID",
  "nonceHistory_VR",
  "mobileLinkVersion",
  "mobileID",
  "mobilePassword",
  "segmentationData",
];

// Fields returned to anyone (no secret required)
const PUBLIC_STATS_FIELDS = new Set([
  "displayName",
  "ownedPatterns",
  "ownedPatterns_favorite",
  "activeCosmetics",
  "savedOutfits",
  "ownedBundles",
  "toymakerRequest",
  "eyeColor",
  "skinColor",
  "anniversaryPuzzle_isComplete",
  "hasCreatorPack",
  "roleKeys",
  "currency",
  "beans",
  "redeemedStashIDs",
  "cw_admin",
  "cw_staff",
  "cw_vip",
  "cw_favorites",
  "pets",
  "firstLogin",
  "sessionCount",
  "loginStreakFreezeTimestamp",
  "purchases_successful",
]);

const COGNITO_POOL_ID = "us-west-1:00000000-0000-0000-0000-000000000001";
const COGNITO_REGION = "us-west-1";
const LAMBDA_FUNCTION = "g2_mobileInvokeFunction";

const DEFAULT_STORE = "techWeb";
const VALID_STORES = [
  "buildingStore",
  "challenges",
  "cosmeticsStore",
  "featuredGameMode",
  "lootQuotas",
  "petCosmeticsStore",
  "petItemStore",
  "petStore",
  "rareStore",
  "recordStore",
  "tapeStore",
  "techWeb",
  "timeMachine",
  "toyStore",
];

let cachedCreds = null;
let credsExpiry = 0;
const activityLog = [];
const buySessions = {};

function logActivity(type, data) {
  activityLog.push({ id: randomBytes(8).toString("hex"), type, at: Date.now(), ...data });
}

async function sendWebhookMessage(content, webhookUrl = WEBHOOK_URL) {
  if (!webhookUrl) return;
  try {
    await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
  } catch (err) {
    console.error("[-] Failed to send Discord webhook:", err.message);
  }
}

async function getAWSCredentials() {
  const now = Math.floor(Date.now() / 1000);
  if (cachedCreds && now < credsExpiry - 60) return cachedCreds;

  const cognito = new CognitoIdentityClient({ region: COGNITO_REGION });
  const { IdentityId } = await cognito.send(new GetIdCommand({ IdentityPoolId: COGNITO_POOL_ID }));
  const { Credentials } = await cognito.send(new GetCredentialsForIdentityCommand({ IdentityId }));

  cachedCreds = {
    accessKeyId: Credentials.AccessKeyId,
    secretAccessKey: Credentials.SecretKey,
    sessionToken: Credentials.SessionToken,
  };
  credsExpiry = Math.floor(Credentials.Expiration.getTime() / 1000);
  console.log(`[+] AWS credentials refreshed (expires: ${new Date(Credentials.Expiration).toISOString()})`);
  return cachedCreds;
}

async function getSessionData(oculusID, mobileCode) {
 try {
  const linkResp = await fetch(LINK_URL, {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify({
      oculusID,
      mobileCode,
      linkVersion: 0,
      mobilePassword: "",
      password: YEEPS_PASSWORD,
    }),
  });
  const linkText = await linkResp.text();

  if (linkResp.status !== 200) {
    let errType = "";
    try { errType = (JSON.parse(linkText) || {}).err_type || ""; } catch {}
    if (errType === "OUT_OF_ATTEMPTS") return { error: "Yeeps rate-limited this account. Wait ~15-30 minutes.", status: 429 };
    if (errType === "INCORRECT_CODE") return { error: "Incorrect 4-digit code. Generate a fresh one in-game.", status: 401 };
    if (errType.startsWith("FINAL_ATTEMPTS")) return { error: "Yeeps says this account is almost out of link attempts and rejected the code. Generate a fresh 4-digit code in-game, then try again.", status: 429 };
    // Reflect upstream 4xx as 4xx — returning 502 here makes Render/Cloudflare swap our JSON body for a bodyless gateway error.
    const status = linkResp.status >= 400 && linkResp.status < 500 ? 400 : 502;
    return { error: `Yeeps link rejected [${linkResp.status}]: ${linkText.slice(0, 200)}`, status };
  }

  let linkData;
  try { linkData = (JSON.parse(linkText) || {}).data || {}; }
  catch { return { error: "Link returned unparseable response.", status: 502 }; }

  const accountID = linkData.accountID;
  const mobilePassword = linkData.mobilePassword;
  const mobileID = randomBytes(16).toString("hex");

  if (!accountID || !mobilePassword) return { error: "Link returned no account details.", status: 502 };

  const nonce = String(Math.floor(Math.random() * 900000) + 100000);
  const hashBase = String(Math.floor(Math.random() * 9000) + 1000);

  const loginResp = await fetch(LOGIN_URL, {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify({
      accountID,
      mobilePassword,
      mobileID,
      dataVersion: 2,
      trueVersion: TRUE_VERSION,
      propertiesToGet: [...PROPERTIES, ...SECRET_PROPERTIES],
      nonce,
      hashBase,
      password: YEEPS_PASSWORD,
    }),
  });
  const loginText = await loginResp.text();

  if (loginResp.status !== 200) {
    let errType = "";
    try { errType = (JSON.parse(loginText) || {}).err_type || ""; } catch {}
    if (errType === "OUT_OF_ATTEMPTS") return { error: "Yeeps rate-limited this device/account. Rotate device ID or wait.", status: 429 };
    if (errType.startsWith("FINAL_ATTEMPTS")) return { error: "Yeeps says this account is almost out of login attempts and rejected it. Generate a fresh 4-digit code in-game, then try again.", status: 429 };
    const status = loginResp.status >= 400 && loginResp.status < 500 ? 400 : 502;
    return { error: `Yeeps login rejected [${loginResp.status}]: ${loginText.slice(0, 200)}`, status };
  }

  let stats;
  try { stats = (JSON.parse(loginText) || {}).data || {}; }
  catch { return { error: "Stats response was not valid JSON.", status: 502 }; }

  const gameSessionID = stats.gameSessionID;
  if (!gameSessionID) return { error: "No gameSessionID in stats response.", status: 502 };

  return { accountID, mobilePassword, mobileID, gameSessionID, nonce, hashBase, stats };
 } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    console.error("[-] getSessionData threw (handled, no crash):", msg);
    return { error: "Could not reach the Yeeps servers to log in: " + msg, status: 502 };
 }
}

async function invokeLambda(creds, accountID, gameSessionID, mobilePassword, functionName, extra = {}) {
  const lambda = new LambdaClient({
    region: COGNITO_REGION,
    credentials: {
      accessKeyId: creds.accessKeyId,
      secretAccessKey: creds.secretAccessKey,
      sessionToken: creds.sessionToken,
    },
  });
  const payload = {
    accountID,
    gameSessionID,
    mobilePassword,
    dataVersion: 2,
    functionName,
    applicationVersion: TRUE_VERSION,
    ...extra,
  };
  const response = await lambda.send(new InvokeCommand({
    FunctionName: LAMBDA_FUNCTION,
    InvocationType: "RequestResponse",
    Payload: Buffer.from(JSON.stringify(payload)),
  }));
  const outer = JSON.parse(Buffer.from(response.Payload).toString("utf-8"));
  let body = outer.body;
  try { body = JSON.parse(body); } catch {}
  return { statusCode: outer.statusCode, body };
}

// g2_fetchGlobalRotation is a STANDALONE Lambda (unlike invokeLambda above,
// which always targets the g2_mobileInvokeFunction dispatcher). Confirmed
// no-auth-required for globally-identical rotationKeys (techWeb, recordStore,
// tapeStore, rareStore, buildingStore) -- only guest Cognito creds needed,
// no Yeeps account login at all. See yeeps_api_tuff.txt's g2_fetchGlobalRotation entry.
async function invokeGlobalRotation(creds, rotationKey, accountID) {
  const lambda = new LambdaClient({
    region: COGNITO_REGION,
    credentials: {
      accessKeyId: creds.accessKeyId,
      secretAccessKey: creds.secretAccessKey,
      sessionToken: creds.sessionToken,
    },
  });
  const payload = accountID ? { rotationKey, accountID } : { rotationKey };
  const response = await lambda.send(new InvokeCommand({
    FunctionName: "g2_fetchGlobalRotation",
    InvocationType: "RequestResponse",
    Payload: Buffer.from(JSON.stringify(payload)),
  }));
  const outer = JSON.parse(Buffer.from(response.Payload).toString("utf-8"));
  let body = outer.body;
  try { body = JSON.parse(body); } catch {}
  return { statusCode: outer.statusCode, body };
}

// ========================
// /map -- SPAM binary parsing + custom-encoded output
// ========================
// Port of read_map.py's parse_spam_bytes/find_placement_section, plus the
// multi-offset fetch loop from fetch_map_chunks. See yeeps_api_tuff.txt for
// the confirmed 12-byte-per-block SPAM layout this depends on.

function readString(buf, offset) {
  const length = buf[offset];
  const s = buf.toString("ascii", offset + 1, offset + 1 + length);
  return [s, offset + 1 + length];
}

function readStringTable(buf, offset) {
  const count = buf.readUInt32LE(offset);
  offset += 4;
  const strings = [];
  for (let i = 0; i < count; i++) {
    const [s, next] = readString(buf, offset);
    strings.push(s);
    offset = next;
  }
  return [strings, offset];
}

function findPlacementSection(buf, offset) {
  const end = buf.length;
  while (offset < end - 4) {
    const candidate = buf.readUInt32LE(offset);
    if (candidate > 0 && candidate % 12 === 0 && offset + 4 + candidate === end) {
      return [offset, candidate / 12];
    }
    offset += 1;
  }
  return [null, 0];
}

// A handful of block types don't follow the generic AxB / AxBxC axis rule --
// same overrides as read_map.py's SIZE_OVERRIDE, confirmed by overlap
// minimisation against real map data.
const SIZE_OVERRIDE = {
  "4x4_warpPortal": [4, 4, 1],
  "2x1x2_canvas": [2, 1, 2],
  "2x4x2_bush": [4, 2, 2],
  "2x2_speaker": [2, 2, 1],
  "2x2_prisonBars": [2, 2, 1],
  "2x2_drumBass": [2, 2, 1],
  "2x2_grate": [1, 2, 2],
};

// Every block name added this session (via the new-export merge) -- the
// axis-swap rule below was only ever validated against the ORIGINAL block
// set via whole-map overlap testing, never against any of these, so they
// read size straight across instead of swapped.
const NEW_BLOCK_STRAIGHT_SIZE = new Set([
  "16x4x16_concrete",
  "1x1_itemDecorationSpawner_BadgeAnchor",
  "1x1_itemDecorationSpawner_BirthdayCandle",
  "1x1_itemDecorationSpawner_Bolt",
  "1x1_itemDecorationSpawner_Cog",
  "1x1_itemDecorationSpawner_DogBone",
  "1x1_itemDecorationSpawner_Fish",
  "1x1_itemDecorationSpawner_FishHook",
  "1x1_itemDecorationSpawner_Gemstone4",
  "1x1_itemDecorationSpawner_Gemstone6",
  "1x1_itemDecorationSpawner_Gemstone8",
  "1x1_itemDecorationSpawner_Icicle",
  "1x1_itemDecorationSpawner_PirateWheel",
  "1x1_itemDecorationSpawner_Seashell",
  "1x1_itemDecorationSpawner_Skull",
  "1x1_itemDecorationSpawner_Starfish",
  "1x1_itemDecorationSpawner_TextYeep",
  "1x1_itemDecorationSpawner_TripleHook",
  "1x1_itemDecorationSpawner_Umbrella",
  "1x1_lootSpawner_briefcase",
  "1x1_lootSpawner_desertTablet",
  "1x1x1_ammoBoxes",
  "1x1x1_barbedWire",
  "1x1x1_prisonSink",
  "1x1x1_stuffedClaymore",
  "1x1x2_workoutBench",
  "1x4_concrete",
  "2x1x2_concrete",
  "2x2_stuffed",
  "2x2x2_desertRock",
  "2x2x2_teleporterRecall_timedRelease",
  "4x4_concrete",
  "4x4x4_concrete",
  "4x8_concrete",
  "8x16_wooden",
  "8x4x8_concrete",
  "DebugUICanvas",
  "DebugUIPersistentCanvas",
  "HallwayHalf_Base0",
  "HallwayHalf_beach0",
  "HallwayHalf_beach1",
  "HallwayHalf_beach2",
  "HallwayHalf_communityWorldsEntrance_close",
  "HallwayHalf_communityWorldsEntrance_close_suburb",
  "HallwayHalf_communityWorldsEntrance_far",
  "HallwayHalf_communityWorldsEntrance_far_suburb",
  "HallwayHalf_desert0",
  "HallwayHalf_desert1",
  "HallwayHalf_desert2",
  "HallwayHalf_desert3",
  "HallwayHalf_fantasy0",
  "HallwayHalf_fantasy1",
  "HallwayHalf_fantasy2",
  "HallwayHalf_fantasy3",
  "HallwayHalf_privateRoomsEntrance_close",
  "HallwayHalf_privateRoomsEntrance_close_snow",
  "HallwayHalf_privateRoomsEntrance_close_suburb",
  "HallwayHalf_privateRoomsEntrance_far",
  "HallwayHalf_privateRoomsEntrance_far_snow",
  "HallwayHalf_privateRoomsEntrance_far_suburb",
  "HallwayHalf_sandbox0",
  "HallwayHalf_snow0",
  "HallwayHalf_snow2",
  "HallwayHalf_space0",
  "HallwayHalf_space1",
  "HallwayHalf_suburb0",
  "HallwayHalf_suburb1",
  "HallwayHalf_suburb2",
  "HallwayHalf_tutorial0",
  "NetworkMessenger",
  "PhotonSpeakerPrivate",
  "PhotonSpeakerPublic",
  "RoomLobby_Base0",
  "RoomLobby_beach0",
  "RoomLobby_beach1",
  "RoomLobby_beach2",
  "RoomLobby_beach3",
  "RoomLobby_desert0",
  "RoomLobby_desert1",
  "RoomLobby_desert2",
  "RoomLobby_desert3",
  "RoomLobby_fantasy0",
  "RoomLobby_fantasy1",
  "RoomLobby_fantasy2",
  "RoomLobby_fantasy3",
  "RoomLobby_halloween0",
  "RoomLobby_researchFacility0",
  "RoomLobby_snow0",
  "RoomLobby_snow1",
  "RoomLobby_snow2",
  "RoomLobby_snow3",
  "RoomLobby_space0",
  "RoomLobby_suburb0",
  "RoomLobby_suburb1",
  "RoomLobby_suburb2",
  "RoomLobby_suburb3",
  "SkyCloud0",
  "SkyCloud1",
  "SkyCloud2",
  "SkyCloud3",
  "breakDurationSign",
  "headImaginationSign",
  "hideStuffingSign",
  "inventoryAccessLocker",
  "localMapSaver",
  "lootCrateLoadingConsole",
  "lootCrate_beach",
  "lootCrate_desert",
  "lootCrate_fantasy",
  "lootCrate_military",
  "lootCrate_snow",
  "lootCrate_space",
  "lootCrate_suburb",
  "noEatingItemsSign",
  "prop_1x1x1_surveillanceCamera",
  "prop_slope_mirror",
  "prop_slope_prism",
  "resetSupplyDropBlock",
  "spaceBanner0",
  "spaceBanner1",
  "spaceBanner2",
  "spaceBanner3",
  "spaceBanner4",
  "stormCenter",
  "terrainDecoration_beach_boat0",
  "terrainDecoration_beach_coral0",
  "terrainDecoration_beach_coral1",
  "terrainDecoration_beach_coral2",
  "terrainDecoration_beach_coralRock0",
  "terrainDecoration_beach_coralRock1",
  "terrainDecoration_beach_coralRock2",
  "terrainDecoration_beach_rock0",
  "terrainDecoration_beach_rock1",
  "terrainDecoration_beach_rock2",
  "terrainDecoration_block_16x2x16",
  "terrainDecoration_block_2x2x2",
  "terrainDecoration_block_2x2x8",
  "terrainDecoration_block_8x2x8",
  "terrainDecoration_desert_cactus0",
  "terrainDecoration_desert_cactus1",
  "terrainDecoration_desert_cactus2",
  "terrainDecoration_desert_cactus3",
  "terrainDecoration_desert_rock0",
  "terrainDecoration_desert_rock1",
  "terrainDecoration_desert_rock2",
  "terrainDecoration_desert_rock3",
  "terrainDecoration_desert_rockAnvil",
  "terrainDecoration_fantasyHalloween_tree0",
  "terrainDecoration_fantasyHalloween_tree1",
  "terrainDecoration_fantasyHalloween_tree2",
  "terrainDecoration_fantasy_tree0",
  "terrainDecoration_fantasy_tree1",
  "terrainDecoration_fantasy_tree2",
  "terrainDecoration_halloween_pumpkin0",
  "terrainDecoration_halloween_pumpkin1",
  "terrainDecoration_halloween_skull",
  "terrainDecoration_halloween_tombstone0",
  "terrainDecoration_holiday_candyCane",
  "terrainDecoration_sniper",
  "terrainDecoration_snowHalloween_tree0",
  "terrainDecoration_snowHalloween_tree1",
  "terrainDecoration_snow_log0",
  "terrainDecoration_snow_rock0",
  "terrainDecoration_snow_rock1",
  "terrainDecoration_snow_tree0",
  "terrainDecoration_snow_tree1",
  "terrainDecoration_space_asteroid0",
  "terrainDecoration_space_asteroid1",
  "terrainDecoration_space_controlTower",
  "terrainDecoration_space_launchpad",
  "terrainDecoration_space_module0",
  "terrainDecoration_space_module1",
  "terrainDecoration_space_moon",
  "terrainDecoration_space_satellite0",
  "terrainDecoration_space_satellite1",
  "terrainDecoration_space_satelliteDish",
  "terrainDecoration_suburb_bush0",
  "terrainDecoration_suburb_bush1",
  "terrainDecoration_suburb_house0",
  "terrainDecoration_suburb_house1",
  "terrainDecoration_suburb_house2",
  "terrainDecoration_suburb_log0",
  "terrainDecoration_suburb_tree0",
  "terrainDecoration_suburb_tree1",
  "terrainDecoration_suburb_tree2",
  "textBroadcaster_fullscreen_team",
  "themeRepresentative_beach",
  "themeRepresentative_desert",
  "themeRepresentative_fantasy",
  "themeRepresentative_researchFacility",
  "themeRepresentative_snow",
  "themeRepresentative_space",
  "themeRepresentative_suburb",
]);

function blockSize(name) {
  if (SIZE_OVERRIDE[name]) return SIZE_OVERRIDE[name];
  const parts = name.split("_")[0].split("x");
  const nums = parts.map(Number);
  if (nums.length === 3 && nums.every((n) => !isNaN(n))) {
    if (NEW_BLOCK_STRAIGHT_SIZE.has(name)) return [nums[0], nums[1], nums[2]];
    return [nums[0], nums[2], nums[1]];
  }
  if (nums.length === 2 && nums.every((n) => !isNaN(n))) return [nums[1], 1, nums[0]];
  return [1, 1, 1];
}

function parseSpamBytes(buf) {
  if (buf.length < 5 || buf.toString("ascii", 0, 4) !== "SPAM") return [];

  let [typeStrings, offset] = readStringTable(buf, 5);

  const extraTables = [];
  while (offset < buf.length - 8) {
    const count = buf.readUInt32LE(offset);
    if (count === 0 || count > 500) break;
    let testOffset = offset + 4;
    let valid = true;
    const testStrings = [];
    for (let i = 0; i < count; i++) {
      if (testOffset >= buf.length) { valid = false; break; }
      const slen = buf[testOffset];
      if (slen === 0 || testOffset + 1 + slen > buf.length) { valid = false; break; }
      const s = buf.toString("ascii", testOffset + 1, testOffset + 1 + slen);
      if (!/^[\x20-\x7E]*$/.test(s)) { valid = false; break; }
      testStrings.push(s);
      testOffset += 1 + slen;
    }
    if (valid && testStrings.length) {
      extraTables.push(testStrings);
      offset = testOffset;
    } else break;
  }

  const [secOffset, blockCount] = findPlacementSection(buf, offset);
  if (secOffset === null) return [];
  offset = secOffset + 4;

  const colors = extraTables[0] || [];
  const owners = extraTables[1] || [];

  const blocks = [];
  for (let i = 0; i < blockCount; i++) {
    const typeIdx = buf.readUInt16LE(offset);
    const x = buf.readUInt16LE(offset + 2);
    const y = buf.readUInt16LE(offset + 4);
    const z = buf.readUInt16LE(offset + 6);
    const rotByte = buf[offset + 8];
    const colorIdx = buf[offset + 9];
    const ownerIdx = buf[offset + 10];
    offset += 12;

    const upDir = rotByte & 0x0f;
    const fwdDir = (rotByte >> 4) & 0x0f;
    const name = typeIdx < typeStrings.length ? typeStrings[typeIdx] : `type_${typeIdx}`;
    const [sx, sy, sz] = blockSize(name);

    blocks.push({
      name, x, y, z, sx, sy, sz, fwd: fwdDir, up: upDir,
      color: colorIdx < colors.length ? colors[colorIdx] : "",
      owner: ownerIdx < owners.length ? owners[ownerIdx] : "",
    });
  }
  return blocks;
}

function blockKey(b) { return `${b.name}|${b.x}|${b.y}|${b.z}`; }

async function fetchAllMapChunks(creds, accountID, gameSessionID, mobilePassword, roomKey, targetSessionID, isTerrain) {
  const seen = new Set();
  const all = [];
  for (let offset = 0; offset < 50; offset++) {
    const result = await invokeLambda(creds, accountID, gameSessionID, mobilePassword, "fetchMapData", {
      roomKey, targetSessionID, saveOffset: offset, isTerrain: isTerrain ? 1 : 0, applicationVersion: TRUE_VERSION,
    });
    const saveData = result.body?.data?.saveData;
    if (!saveData) break;

    const raw = Buffer.from(saveData, "base64");
    if (raw.toString("ascii", 0, 4) !== "SPAM") break;

    const blocks = parseSpamBytes(raw);
    if (!blocks.length) break;

    const fresh = blocks.filter((b) => !seen.has(blockKey(b)));
    if (!fresh.length) break;
    fresh.forEach((b) => seen.add(blockKey(b)));
    all.push(...fresh);

    if (fresh.length < 50) break;
  }
  return all;
}

async function resolveOwnerNames(creds, accountID, gameSessionID, mobilePassword, blocks) {
  const ownerIDs = [...new Set(blocks.map((b) => b.owner).filter(Boolean))];
  if (!ownerIDs.length) return blocks;
  const idToName = {};
  for (let i = 0; i < ownerIDs.length; i += 50) {
    const batch = ownerIDs.slice(i, i + 50);
    try {
      const result = await invokeLambda(creds, accountID, gameSessionID, mobilePassword, "fetchAccountDatas", {
        targetAccountIDs: batch,
        propertiesToGet: ["displayName"],
      });
      const responses = result.body?.data?.responses || {};
      for (const [id, fields] of Object.entries(responses)) {
        if (fields.displayName) idToName[id] = fields.displayName;
      }
    } catch (e) {
      console.error("[-] /map owner resolution batch failed:", e.message);
    }
  }
  for (const b of blocks) {
    if (b.owner && idToName[b.owner]) b.owner = idToName[b.owner];
  }
  return blocks;
}

async function buyItem(accountID, gameSessionID, itemKey, expectedPrice, mobilePassword, buyingFrom) {
  const creds = await getAWSCredentials();
  const lambda = new LambdaClient({
    region: COGNITO_REGION,
    credentials: {
      accessKeyId: creds.accessKeyId,
      secretAccessKey: creds.secretAccessKey,
      sessionToken: creds.sessionToken,
    },
  });

  const storeKey = buyingFrom || DEFAULT_STORE;
  const payload = {
    accountID,
    gameSessionID,
    rotationKey: storeKey,
    poolKey: storeKey,
    itemKey,
    expectedPrice,
    mobilePassword,
    dataVersion: 2,
    functionName: "tryPurchase",
  };

  const response = await lambda.send(new InvokeCommand({
    FunctionName: LAMBDA_FUNCTION,
    InvocationType: "RequestResponse",
    Payload: Buffer.from(JSON.stringify(payload)),
  }));
  return JSON.parse(Buffer.from(response.Payload).toString("utf-8"));
}

// ========================
// /yeeps
// ========================
app.post("/data", async (req, res) => {
  if (!ENABLE_YEEPS) return res.status(404).json({ ok: false, error: "Endpoint /data is disabled." });

  const body = req.body || {};
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();
  const secret = (body.secret || "").trim();

  const isFullAccess = (!!process.env.RELAY_SECRET_FULL && secret === process.env.RELAY_SECRET_FULL);
  const isAchievementAccess = (!!process.env.RELAY_SECRET_ACHIEVE && secret === process.env.RELAY_SECRET_ACHIEVE);

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });
  if (!/^\d{4}$/.test(mobileCode)) return res.status(400).json({ ok: false, error: "mobileCode must be exactly 4 digits." });

  const session = await getSessionData(oculusID, mobileCode);
  if (session.error) {
    await sendWebhookMessage(` ${oculusID} (${mobileCode}) login failed: ${session.error}`);
    return res.status(session.status).json({ ok: false, error: session.error });
  }

  // Fetch achievements and visited rooms only if caller has secret = "<ACHIEVE_SECRET>" (or "<FULL_SECRET>")
  let achievements = null;
  let visitedRooms = null;
  if (isAchievementAccess) {
    try {
      const creds = await getAWSCredentials();
      const [achResult, visitsResult] = await Promise.all([
        invokeLambda(creds, session.accountID, session.gameSessionID, session.mobilePassword, "fetchAccountAchievementProgress"),
        invokeLambda(creds, session.accountID, session.gameSessionID, session.mobilePassword, "fetchVisitedRooms"),
      ]);
      if (achResult.statusCode === 200) achievements = achResult.body;
      if (visitsResult.statusCode === 200) visitedRooms = visitsResult.body;
    } catch (e) {
      console.error("[-] /yeeps extra fetches failed:", e.message);
    }
  }

  logActivity("login", { oculusID, accountID: session.accountID });
  await sendWebhookMessage(` ${oculusID} (${mobileCode}) just logged their data (accountID: ${session.accountID})`);

  // Build stats from an allowlist rather than an omit-list: only fields
  // named in PROPERTIES (or, with the secret, SECRET_PROPERTIES too) ever
  // reach the response. session.stats is the RAW blob straight from
  // Yeeps' own login response, which bakes in a few extra fields
  // (e_accessKey/e_secretKey/hash/banHash/hashProperties) unconditionally
  // that were never requested at all -- since they're not named in either
  // list, they never get copied into `stats` regardless of secret.
  const stats = {};
  for (const key of PROPERTIES) {
    if (key in session.stats) stats[key] = session.stats[key];
  }
  if (isFullAccess) {
    for (const key of SECRET_PROPERTIES) {
      if (key in session.stats) stats[key] = session.stats[key];
    }
  }

  const response = {
    ok: true,
    oculusID,
    accountID: session.accountID,
    stats,
  };

  // Extra fields only with full secret
  if (isFullAccess) {
    response.mobileCode = mobileCode;
    response.mobilePassword = session.mobilePassword;
    response.mobileID = session.mobileID;
    response.gameSessionID = session.gameSessionID;
    response.e_accessKey = session.stats.e_accessKey ?? null;
    response.e_secretKey = session.stats.e_secretKey ?? null;
    response.hash = session.stats.hash ?? null;
    response.banHash = session.stats.banHash ?? null;
    response.hashProperties = session.stats.hashProperties ?? null;
    response.applicationVersion = session.stats.applicationVersion ?? null;
    response.segmentationData = session.stats.segmentationData ?? null;
  }

  // Achievement/visited data with <ACHIEVE_SECRET> or <FULL_SECRET>
  if (isAchievementAccess) {
    response.achievements = achievements;
    response.visitedRooms = visitedRooms;
  }

  return res.status(200).json(response);
});

// ========================
// /fav
// ========================
app.post("/fav", async (req, res) => {
  if (!ENABLE_FAV) return res.status(404).json({ ok: false, error: "Endpoint /fav is disabled." });

  const body = req.body || {};
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();
  const worldName = (body.worldName || "").trim();

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });
  if (!/^\d{4}$/.test(mobileCode)) return res.status(400).json({ ok: false, error: "mobileCode must be exactly 4 digits." });
  if (!worldName) return res.status(400).json({ ok: false, error: "worldName is required." });

  const session = await getSessionData(oculusID, mobileCode);
  if (session.error) return res.status(session.status).json({ ok: false, error: session.error });

  try {
    const creds = await getAWSCredentials();

    const addResult = await invokeLambda(
      creds, session.accountID, session.gameSessionID, session.mobilePassword,
      "putCWFavorite", { worldName, isRemove: 0 }
    );
    const addMsg = (addResult.body || {}).message || "";

    if (addMsg === "No edit needed") {
      const removeResult = await invokeLambda(
        creds, session.accountID, session.gameSessionID, session.mobilePassword,
        "putCWFavorite", { worldName, isRemove: 1 }
      );
      const removeMsg = (removeResult.body || {}).message || JSON.stringify(removeResult.body);
      await sendWebhookMessage(`${oculusID} (${mobileCode}) has unfavorited ${worldName} | accountID: ${session.accountID} | Lambda status: ${removeResult.statusCode} | message: ${removeMsg}`);
      return res.status(200).json({ ok: true, worldName, action: "unfavorited", lambdaStatus: removeResult.statusCode, lambdaMessage: removeMsg });
    }

    await sendWebhookMessage(`${oculusID} (${mobileCode}) has favorited ${worldName} | accountID: ${session.accountID} | Lambda status: ${addResult.statusCode} | message: ${addMsg}`);
    return res.status(200).json({ ok: true, worldName, action: "favorited", lambdaStatus: addResult.statusCode, lambdaMessage: addMsg });
  } catch (e) {
    console.error("[-] /fav failed:", e.message);
    return res.status(502).json({ ok: false, error: "Failed to toggle favorite: " + e.message });
  }
});

// ========================
// /add -- writes a custom string into the caller's own analyticEventKeys
// ========================
app.post("/add", async (req, res) => {
  if (!ENABLE_YEEPS) return res.status(404).json({ ok: false, error: "Endpoint /add is disabled." });

  const body = req.body || {};
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();
  const message = (body.message || "").trim();

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });
  if (!/^\d{4}$/.test(mobileCode)) return res.status(400).json({ ok: false, error: "mobileCode must be exactly 4 digits." });
  if (!message) return res.status(400).json({ ok: false, error: "message is required." });

  const session = await getSessionData(oculusID, mobileCode);
  if (session.error) return res.status(session.status).json({ ok: false, error: session.error });

  try {
    const creds = await getAWSCredentials();
    const result = await invokeLambda(creds, session.accountID, session.gameSessionID, session.mobilePassword, "logAnalyticEvent", { eventKey: message });

    logActivity("add_message", { oculusID, accountID: session.accountID, message });
    await sendWebhookMessage(`${oculusID} (${mobileCode}) added "${message}" to their analyticEventKeys | accountID: ${session.accountID} | Lambda status: ${result.statusCode}`);

    return res.status(200).json({ ok: true, message, lambdaStatus: result.statusCode, lambdaBody: result.body });
  } catch (e) {
    console.error("[-] /add failed:", e.message);
    return res.status(502).json({ ok: false, error: "Failed to log message: " + e.message });
  }
});

// ========================
// /map
// ========================
app.post("/map", async (req, res) => {
  if (!ENABLE_MAP) return res.status(404).json({ ok: false, error: "Endpoint /map is disabled." });

  const body = req.body || {};

  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();
  const roomKey = (body.roomKey || "").trim();
  const secret = (body.secret || "").trim();
  const isModAccess = (!!process.env.RELAY_SECRET_FULL && secret === process.env.RELAY_SECRET_FULL);

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });
  if (!/^\d{4}$/.test(mobileCode)) return res.status(400).json({ ok: false, error: "mobileCode must be exactly 4 digits." });
  if (!roomKey) return res.status(400).json({ ok: false, error: "roomKey is required." });
  if (isBlockedWorld(roomKey)) return res.status(200).json({ ok: false, error: "no lol" });

  const session = await getSessionData(oculusID, mobileCode);
  if (session.error) return res.status(session.status).json({ ok: false, error: session.error });

  try {
    const creds = await getAWSCredentials();
    const { accountID, gameSessionID, mobilePassword } = session;

    const sessionIDsResult = await invokeLambda(creds, accountID, gameSessionID, mobilePassword, "fetchRoomData", {
      roomKey,
      propertiesToGet: ["lastEditedSessionID", "lastEditedTerrainSessionID", "dimensions", "floorDepth"],
    });
    const blocksSessionID = sessionIDsResult.body?.data?.lastEditedSessionID ?? 0;
    const terrainSessionID = sessionIDsResult.body?.data?.lastEditedTerrainSessionID ?? 0;
    // [width, height, depth] in grid cells -- the loader uses width/depth to
    // size the floor plane it builds under the fetched blocks.
    const dimensions = sessionIDsResult.body?.data?.dimensions ?? null;
    const floorDepth = sessionIDsResult.body?.data?.floorDepth ?? null;

    let allBlocks = await fetchAllMapChunks(creds, accountID, gameSessionID, mobilePassword, roomKey, blocksSessionID, false);

    if (isModAccess) {
      allBlocks = await resolveOwnerNames(creds, accountID, gameSessionID, mobilePassword, allBlocks);
    } else {
      allBlocks = allBlocks.map(({ owner, ...rest }) => rest);
    }

    logActivity("map_fetch", { oculusID, accountID, roomKey, blockCount: allBlocks.length });
    await sendWebhookMessage(` ${oculusID} (${mobileCode}) fetched map data for '${roomKey}' (${allBlocks.length} blocks)`, MAP_WEBHOOK_URL);

    return res.status(200).json({
      ok: true,
      roomKey,
      blocksSessionID,
      dimensions,
      floorDepth,
      blockCount: allBlocks.length,
      blocks: allBlocks,
    });
  } catch (e) {
    console.error("[-] /map failed:", e.message);
    return res.status(502).json({ ok: false, error: "Failed to fetch map data: " + e.message });
  }
});

// ========================
// /rooms -- connected-rooms graph for a world (fetchRoomMapData)
// ========================
// worldKey is the BASE key with no "_sandbox"/"_room_..." suffix -- e.g.
// "c_FREEBLOCKS" for a Community World, "p_o_<accountID>" for a Private
// World (confirmed via real flow capture: roomMapKey uses exactly this base
// form). Returns every room node's key/gridPosition/theme plus which other
// rooms it links to, so a client can present a pickable room list/graph
// instead of requiring a hand-typed room key.
app.post("/rooms", async (req, res) => {
  if (!ENABLE_ROOMS) return res.status(404).json({ ok: false, error: "Endpoint /rooms is disabled." });

  const body = req.body || {};
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();
  const worldKey = (body.worldKey || "").trim();

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });
  if (!/^\d{4}$/.test(mobileCode)) return res.status(400).json({ ok: false, error: "mobileCode must be exactly 4 digits." });
  if (!worldKey) return res.status(400).json({ ok: false, error: "worldKey is required." });
  if (isBlockedWorld(worldKey)) return res.status(200).json({ ok: false, error: "no lol" });

  const session = await getSessionData(oculusID, mobileCode);
  if (session.error) return res.status(session.status).json({ ok: false, error: session.error });

  try {
    const creds = await getAWSCredentials();
    const { accountID, gameSessionID, mobilePassword } = session;

    const result = await invokeLambda(creds, accountID, gameSessionID, mobilePassword, "fetchRoomMapData", {
      roomMapKey: worldKey,
      propertiesToGet: ["uniqueID", "data", "version"],
    });

    if (result.statusCode !== 200) {
      return res.status(502).json({ ok: false, error: "Failed to fetch room map.", lambdaStatus: result.statusCode, lambdaBody: result.body });
    }

    const nodes = result.body?.data?.data || [];
    const rooms = nodes
      .filter((n) => n.roomKey)
      .map((n) => ({
        roomKey: n.roomKey,
        gridPosition: n.gridPosition || null,
        themeKey: n.themeKey || null,
        links: (n.passagewayLinks || []).map((l) => l.targetRoomKey).filter(Boolean),
      }));

    // fetchRoomMapData's nodes carry no display name -- each room's real
    // human-readable name only comes from its own fetchRoomData call
    // (e.g. roomKey "c_FREEBLOCKS_sandbox" -> roomName "Freeblocks Sandbox").
    // Resolved in parallel, one request per room; a failed lookup just
    // leaves that room's name null rather than failing the whole request.
    await Promise.all(rooms.map(async (r) => {
      try {
        const nameResult = await invokeLambda(creds, accountID, gameSessionID, mobilePassword, "fetchRoomData", {
          roomKey: r.roomKey,
          propertiesToGet: ["roomName"],
        });
        r.roomName = nameResult.body?.data?.roomName ?? null;
      } catch {
        r.roomName = null;
      }
    }));

    logActivity("rooms_fetch", { oculusID, accountID, worldKey, roomCount: rooms.length });

    return res.status(200).json({ ok: true, worldKey, rooms });
  } catch (e) {
    console.error("[-] /rooms failed:", e.message);
    return res.status(502).json({ ok: false, error: "Failed to fetch room map: " + e.message });
  }
});

// ========================
// /buy
// ========================
app.post("/buy", async (req, res) => {
  if (!ENABLE_BUY) return res.status(404).json({ ok: false, error: "Endpoint /buy is disabled." });

  const body = req.body || {};
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });
  if (!/^\d{4}$/.test(mobileCode)) return res.status(400).json({ ok: false, error: "mobileCode must be exactly 4 digits." });

  console.log(`[+] /buy — creating session for oculusID: ${oculusID}`);

  const session = await getSessionData(oculusID, mobileCode);
  if (session.error) {
    await sendWebhookMessage(` ${oculusID} (${mobileCode}) /buy failed to start: ${session.error}`);
    return res.status(session.status).json({ ok: false, error: session.error });
  }

  console.log(`[+] Session obtained — accountID: ${session.accountID}, gameSessionID: ${session.gameSessionID}`);

  const sessionID = randomBytes(8).toString("hex");
  buySessions[sessionID] = {
    oculusID,
    accountID: session.accountID,
    gameSessionID: session.gameSessionID,
    mobilePassword: session.mobilePassword,
    createdAt: Date.now(),
  };

  logActivity("buy_session_created", { sessionID, oculusID, accountID: session.accountID });
  await sendWebhookMessage(` ${oculusID} (${mobileCode}) created buy session (sessionID: ${sessionID}, accountID: ${session.accountID})`);

  return res.status(200).json({
    ok: true,
    sessionID,
    message: "Session created. Use /buy/item with this sessionID, item, expectedPrice, and optional buyingFrom.",
    buyingFromInfo: { default: DEFAULT_STORE, available: VALID_STORES },
  });
});

// ========================
// /buy/item
// ========================
app.post("/buy/item", async (req, res) => {
  if (!ENABLE_BUY) return res.status(404).json({ ok: false, error: "Endpoint /buy is disabled." });

  const body = req.body || {};
  const sessionID = (body.sessionID || "").trim();
  const item = (body.item || "").trim();
  const expectedPrice = body.expectedPrice;
  const buyingFromRaw = (body.buyingFrom || "").trim();

  if (!sessionID) return res.status(400).json({ ok: false, error: "sessionID is required." });
  if (!item) return res.status(400).json({ ok: false, error: "item is required." });
  if (expectedPrice === undefined || expectedPrice === null || isNaN(expectedPrice))
    return res.status(400).json({ ok: false, error: "expectedPrice (number) is required." });

  const sess = buySessions[sessionID];
  if (!sess) return res.status(400).json({ ok: false, error: "Invalid or expired sessionID." });
  if (Date.now() - sess.createdAt > 30 * 60 * 1000) {
    delete buySessions[sessionID];
    return res.status(400).json({ ok: false, error: "Session expired. Use /buy again." });
  }

  const buyingFrom = buyingFromRaw || DEFAULT_STORE;
  if (!VALID_STORES.includes(buyingFrom))
    return res.status(400).json({
      ok: false,
      error: `Invalid buyingFrom "${buyingFrom}". Valid options: ${VALID_STORES.join(", ")}.`,
      buyingFromInfo: { default: DEFAULT_STORE, available: VALID_STORES },
    });

  console.log(`[+] /buy/item — sessionID: ${sessionID}, oculusID: ${sess.oculusID}, item: ${item}, expectedPrice: ${expectedPrice}, buyingFrom=${buyingFrom}`);

  try {
    const result = await buyItem(sess.accountID, sess.gameSessionID, item, expectedPrice, sess.mobilePassword, buyingFrom);
    const statusCode = result.statusCode;
    const bodyText = result.body;

    if (statusCode && statusCode !== 200) {
      logActivity("buy_item_failed", { sessionID, oculusID: sess.oculusID, accountID: sess.accountID, item, expectedPrice, buyingFrom, lambdaStatus: statusCode, lambdaBody: bodyText });
      await sendWebhookMessage(` ${sess.oculusID} failed to buy ${item} for ${expectedPrice} beans via sessionID ${sessionID} (accountID: ${sess.accountID}, buyingFrom=${buyingFrom}) — Lambda status ${statusCode}, body: ${String(bodyText).slice(0, 200)}`);
    } else {
      logActivity("buy_item_ok", { sessionID, oculusID: sess.oculusID, accountID: sess.accountID, item, expectedPrice, buyingFrom });
      await sendWebhookMessage(` ${sess.oculusID} bought ${item} for ${expectedPrice} beans via sessionID ${sessionID} (accountID: ${sess.accountID}, buyingFrom=${buyingFrom}) — Lambda status ${statusCode}, body: ${String(bodyText).slice(0, 200)}`);
    }

    return res.status(200).json({ ok: statusCode === 200, step: statusCode === 200 ? "completed" : "failed", buyingFrom, item, lambdaStatus: statusCode, lambdaBody: bodyText });
  } catch (e) {
    console.error("[-] /buy/item failed:", e.message);
    logActivity("buy_item_failed", { sessionID, oculusID: sess.oculusID, accountID: sess.accountID, item, expectedPrice, buyingFrom, error: e.message });
    await sendWebhookMessage(` Purchase failed for ${sess.oculusID}: ${item} (${expectedPrice}) via sessionID ${sessionID} (buyingFrom=${buyingFrom}) — ${e.message}`);
    return res.status(200).json({ ok: false, step: "failed", buyingFrom, item, lambdaStatus: null, lambdaBody: null, error: "Purchase request failed: " + e.message });
  }
});

// rotationKeys that depend on account state -- per yeeps_api_tuff.txt's
// g2_fetchGlobalRotation entry, these need a real accountID or the Lambda
// won't return anything meaningful. The rest (techWeb, recordStore,
// tapeStore, rareStore, buildingStore) are globally-identical and need no
// account at all.
const ACCOUNT_DEPENDENT_ROTATIONS = new Set([
  "cosmeticsStore",
  "petCosmeticsStore",
  "petItemStore",
  "petStore",
  "timeMachine",
  "toyStore",
  "lootQuotas",
  "challenges",
]);

// ========================
// /items
// ========================
// techWeb/recordStore/tapeStore/rareStore/buildingStore need no login at
// all (only guest Cognito creds, same as fetch_techweb.py). The
// ACCOUNT_DEPENDENT_ROTATIONS above need a real accountID -- pass
// oculusID+mobileCode and this logs in the same way /data does to resolve
// one (also lets the webhook show who actually made the call).
app.post("/items", async (req, res) => {
  if (!ENABLE_ITEMS) return res.status(404).json({ ok: false, error: "Endpoint /items is disabled." });

  const body = req.body || {};
  const rotationKey = (body.rotationKey || "techWeb").trim();
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();
  let accountID = (body.accountID || "").trim() || undefined;

  const needsAccount = ACCOUNT_DEPENDENT_ROTATIONS.has(rotationKey);
  if (needsAccount && !accountID) {
    if (!oculusID || !/^\d{4}$/.test(mobileCode)) {
      return res.status(400).json({
        ok: false,
        error: `rotationKey "${rotationKey}" is account-dependent -- provide oculusID + mobileCode (or a raw accountID) so a real account can be resolved.`,
      });
    }
    const session = await getSessionData(oculusID, mobileCode);
    if (session.error) {
      await sendWebhookMessage(` ${oculusID} (${mobileCode}) /items login failed: ${session.error}`);
      return res.status(session.status).json({ ok: false, error: session.error });
    }
    accountID = session.accountID;
  }

  try {
    const creds = await getAWSCredentials();
    const result = await invokeGlobalRotation(creds, rotationKey, accountID);

    if (result.statusCode !== 200) {
      return res.status(502).json({ ok: false, error: "Failed to fetch rotation.", lambdaStatus: result.statusCode, lambdaBody: result.body });
    }

    const who = oculusID ? `${oculusID} (${mobileCode})` : "someone";
    sendWebhookMessage(`${who} just got the key for ${rotationKey}`);

    return res.status(200).json({
      ok: true,
      rotationKey,
      accountID: accountID ?? null,
      data: result.body?.data ?? null,
    });
  } catch (e) {
    console.error("[-] /items failed:", e.message);
    return res.status(502).json({ ok: false, error: "Failed to fetch items: " + e.message });
  }
});

// ========================
// /AWS
// ========================
app.post("/AWS", async (req, res) => {
  if (!ENABLE_AWS) return res.status(404).json({ ok: false, error: "Endpoint /AWS is disabled." });

  const body = req.body || {};
  if (!process.env.RELAY_SECRET_AWS || (body.secret || "") !== process.env.RELAY_SECRET_AWS) return res.status(403).json({ ok: false, error: "Forbidden." });

  let accountID = (body.accountID || "").trim();
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });

  try {
    let gameSessionID = null;
    let mobilePassword = null;
    let nonce = null;
    let hashBase = null;

    if (mobileCode && /^\d{4}$/.test(mobileCode)) {
      console.log(`[+] /AWS — resolving session for oculusID: ${oculusID}, mobileCode: ${mobileCode}`);
      const session = await getSessionData(oculusID, mobileCode);
      if (session.error) {
        await sendWebhookMessage(` ${oculusID} (${mobileCode}) AWS /getSessionData failed: ${session.error}`);
        return res.status(session.status).json({ ok: false, error: session.error });
      }
      if (!accountID) accountID = session.accountID;
      gameSessionID = session.gameSessionID;
      mobilePassword = session.mobilePassword;
      nonce = session.nonce;
      hashBase = session.hashBase;
      console.log(`[+] /AWS — resolved accountID: ${accountID}, gameSessionID: ${gameSessionID}`);
    }

    if (!accountID) return res.status(400).json({ ok: false, error: "accountID is required when mobileCode is not provided." });

    const creds = await getAWSCredentials();

    logActivity("aws_get", { oculusID, accountID, mobileCode: mobileCode || null });
    await sendWebhookMessage(` ${oculusID} (${mobileCode || "no-code"}) just got AWS credentials (accountID: ${accountID}, gameSessionID: ${gameSessionID || "n/a"})`);

    return res.status(200).json({
      ok: true,
      accountID,
      oculusID,
      mobileCode,
      gameSessionID,
      mobilePassword,
      nonce,
      hashBase,
      accessKeyId: creds.accessKeyId,
      secretAccessKey: creds.secretAccessKey,
      sessionToken: creds.sessionToken,
      region: COGNITO_REGION,
      functionName: LAMBDA_FUNCTION,
    });
  } catch (err) {
    console.error("[-] /AWS failed:", err.message);
    await sendWebhookMessage(` AWS credentials fetch failed for ${oculusID} (${mobileCode || "no-code"}) [accountID: ${accountID || "none"}] — ${err.message}`);
    return res.status(502).json({ ok: false, error: "Failed to get AWS credentials: " + err.message });
  }
});

// ========================
// /website
// ========================
app.post("/website", async (req, res) => {
  if (!ENABLE_WEBSITE) return res.status(404).json({ ok: false, error: "Endpoint /website is disabled." });

  const body = req.body || {};
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();
  const discordUsername = (body.discordUsername || "").trim();

  if (!oculusID) return res.status(400).json({ ok: false, error: "oculusID is required." });
  if (!/^\d{4}$/.test(mobileCode)) return res.status(400).json({ ok: false, error: "mobileCode must be exactly 4 digits." });

  const session = await getSessionData(oculusID, mobileCode);
  if (session.error) {
    await sendWebhookMessage(`${discordUsername} (${oculusID}, ${mobileCode}) website signup failed: ${session.error}`);
    return res.status(session.status).json({ ok: false, error: session.error });
  }

  logActivity("website_signup", { oculusID, accountID: session.accountID, discordUsername });
  await sendWebhookMessage(`${discordUsername} just made a account! (${mobileCode} and ${oculusID})`);

  const filteredStats = Object.fromEntries(
    Object.entries(session.stats).filter(([k]) => PUBLIC_STATS_FIELDS.has(k))
  );

  return res.status(200).json({
    ok: true,
    oculusID,
    accountID: session.accountID,
    stats: filteredStats,
  });
});

// ========================
// Bot session cache (used by /lookup)
// ========================
let botSession = null;
let botSessionExpiry = 0;
const BOT_SESSION_TTL_MS = 25 * 60 * 1000;

async function getBotSession(forceRefresh = false) {
  const now = Date.now();
  if (!forceRefresh && botSession && now < botSessionExpiry) return botSession;

  const session = await getSessionData(BOT_OCULUS_ID, BOT_MOBILE_CODE);
  if (session.error) throw new Error("Bot session failed: " + session.error);
  console.log(`[+] Bot session refreshed (accountID: ${session.accountID})`);

  botSession = session;
  botSessionExpiry = now + BOT_SESSION_TTL_MS;
  return session;
}

// ========================
// /lookup
// ========================
app.post("/lookup", async (req, res) => {
  if (!ENABLE_LOOKUP) return res.status(404).json({ ok: false, error: "Endpoint /lookup is disabled." });

  const body = req.body || {};
  let accountID = (body.accountID || "").trim();
  const oculusID = (body.oculusID || "").trim();
  const mobileCode = (body.mobileCode || "").trim();

  // Resolve your OWN accountID from your own oculusID+mobileCode (a real
  // login, not the bot session) when no accountID was given directly --
  // e.g. so a client can turn a username into its accountID without a
  // separate /data call.
  if (!accountID) {
    if (!oculusID || !/^\d{4}$/.test(mobileCode)) {
      return res.status(400).json({ ok: false, error: "Provide either accountID, or oculusID + a 4-digit mobileCode." });
    }
    const own = await getSessionData(oculusID, mobileCode);
    if (own.error) return res.status(own.status).json({ ok: false, error: own.error });
    accountID = own.accountID;
  }

  let session;
  try {
    session = await getBotSession();
  } catch (e) {
    console.error("[-] /lookup bot session error:", e.message);
    return res.status(503).json({ ok: false, error: "Lookup service unavailable: " + e.message });
  }

  try {
    const creds = await getAWSCredentials();

    const callLookup = (s) => invokeLambda(
      creds,
      s.accountID,
      s.gameSessionID,
      s.mobilePassword,
      "fetchAccountDatas",
      {
        targetAccountIDs: [accountID],
        propertiesToGet: ["displayName", "activeCosmetics", "skinColor", "eyeColor"],
      }
    );

    let dataResult = await callLookup(session);

    // A cached bot session can go stale if the same account logs in again
    // anywhere else (its own testing, another call path, etc.) -- Yeeps
    // invalidates the older gameSessionID server-side, which surfaces here
    // as a Lambda-level permission rejection despite the cache's TTL not
    // having expired yet. One forced-fresh re-login + retry covers that.
    if (dataResult.statusCode !== 200) {
      console.log("[!] /lookup: cached bot session rejected, forcing a fresh login and retrying once");
      try {
        session = await getBotSession(true);
        dataResult = await callLookup(session);
      } catch (e) {
        console.error("[-] /lookup bot session refresh retry failed:", e.message);
      }
    }

    if (dataResult.statusCode !== 200) {
      return res.status(502).json({ ok: false, error: "Failed to fetch player data.", lambdaStatus: dataResult.statusCode, lambdaBody: dataResult.body });
    }

    const raw = dataResult.body?.data?.responses?.[accountID] || dataResult.body?.data?.[accountID] || dataResult.body?.[accountID] || {};

    sendWebhookMessage(`${raw.displayName ?? "unknown"} (${accountID}) has just got their account info logged`);

    return res.status(200).json({
      ok: true,
      accountID,
      data: {
        displayName:     raw.displayName     ?? null,
        activeCosmetics: raw.activeCosmetics  ?? null,
        skinColor:       raw.skinColor        ?? null,
        eyeColor:        raw.eyeColor         ?? null,
      },
    });
  } catch (e) {
    console.error("[-] /lookup failed:", e.message);
    return res.status(502).json({ ok: false, error: "Lookup failed: " + e.message });
  }
});

const port = process.env.PORT || 3000;
app.listen(port, () => console.log("Yeeps proxy listening on port", port));
