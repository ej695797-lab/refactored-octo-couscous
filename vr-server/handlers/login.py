"""
Handler for g2_questLogIntoAccount.
Auto-creates accounts on first login.
"""

import os
import uuid
import secrets
import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from services.db import accounts
from services.property_defaults import default_for_property
from services.cognito_creds import get_guest_credentials
from services.build_gate import outdated_message
from services.kick import DEFAULT_KICK_REASON

LOGIN_LOG_FILE = Path("logs/login_traffic.log")

# Keep references to fire-and-forget alert tasks so they aren't GC'd mid-flight.
_bg_tasks: set = set()


async def _alert_new_ip(account_id: str, name, oculus, new_ip: str, prior_ips: list) -> None:
    """Post an ADMIN-webhook alert when an account logs in from an IP we've never
    seen for it. ALERT ONLY — no auto-ban (a changed IP is usually a legit player
    on a new network, and IP bans hit everyone sharing that IP). The admin can
    ban the IP from the editor if it's actually account theft.
    Off unless NEW_IP_ALERTS=1 (owner 2026-11-01: too noisy, buried the editor logs)."""
    if os.getenv("NEW_IP_ALERTS", "0") != "1":
        return
    try:
        import re as _re
        import httpx
        from config import ADMIN_DISCORD_WEBHOOK, labeled
        who = _re.sub(r"<[^>]+>", "", str(name or "")).strip() or str(oculus or account_id)
        who = _re.sub(r"([*_`~|\\])", r"\\\1", who)  # neutralize Discord markdown
        seen = ", ".join(f"`{p}`" for p in dict.fromkeys(prior_ips) if p) or "(none)"
        msg = (f"New IP for **{who}** (`{account_id}`): `{new_ip}`\n"
               f"not in recent IPs: {seen}\n"
               f"Review — if this isn't them, ban the IP from the editor. "
               f"(A changed IP is usually just a new network; this is only a heads-up.)")
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(ADMIN_DISCORD_WEBHOOK, json={"content": labeled(msg)[:1900]})
    except Exception as _e:
        print(f"[login] new-IP alert failed for {account_id}: {_e}")

# Every cosmetic item key found in the client's string table (hats, masks,
# heads, and toy/tool skins). Granted to every account so everything is
# owned -- this is a private server, so there's no catalog endpoint to pull
# the "real" list from.
ALL_OWNED_ITEMS = [
    "RCAdvancedCar_blue", "RCAdvancedCar_green", "RCAdvancedCar_red", "anglerfishHat",
    "babyPenguinHat", "bakerBoyHat", "blobfishHat", "brainJarHat", "bucketHat", "candleHat",
    "capBackwardsHat", "capForwardsHat", "capJuiceHat", "catEyeMask", "catHat", "cheeseHat",
    "christmasWreathHat", "constructionConeHat", "constructionHardHat", "cookedTurkeyHat",
    "cookingPotHat", "crownHat", "cupPongHat", "devilHornsHat", "devonMask", "fancyTopHat",
    "frenchHat", "fuzzHat", "grapplingHook_spider", "grouchoHat", "haloHat",
    "hammerheadSharkHead", "headphonesHead", "horseHat", "iceSkateHat", "innerTube_duck",
    "jesterHat", "kelpCrownHat", "laurelHat", "lionHead", "machete_pirate", "medusaHat",
    "meltingSnowmanHat", "mummyHat", "mushroomHat", "neckTieHat", "octopusHat", "oniMask",
    "pancakeHat", "paperBagHat", "partyHat", "pirateFacialHair", "pizzaBoxHat",
    "pottedPlantHat", "presentHat", "rangerHat", "sailorHat", "scarecrowHat",
    "scaryHockeyMask", "seashellHat", "sharkBiteHat", "sharkFinHat", "sirenHat",
    "sittingTurkeyHat", "skiMaskHat", "snowmanBeanieHat", "spiderHat", "sunflowerHat",
    "takeoutBoxHat", "tropicalFlowerHat", "werewolfHead", "winterFlannelHat",
    "winterHelmetHat", "winterStripedHat", "wizardHat", "woodReindeerHat", "zipperSkiMask",
    "zipperVoodooHead",
]
ALL_OWNED_BUNDLES = ["bundle_commando"]
ALL_ROLE_KEYS = ["developer", "developer_senior", "moderator", "moderator_junior", "tester"]
# techWeb build-menu items that exist in the 1.19.0 client. The original list
# (1479 keys) was captured from the live 2.x game; 1.19.0 rejects the other
# 1056 ("Owned pattern key X does not exist"), verified on-device.
ALL_TECHWEB_ITEMS = [
    '16x16_teamBarrier',
    '16x16_teamBuildPlate',
    '16x16x4_beachRock',
    '16x16x4_grassRock',
    '16x16x4_grassRockUncovered',
    '16x16x4_sand',
    '16x16x4_snowRock',
    '16x16x4_snowRockUncovered',
    '1x1_3way_corner_pipe',
    '1x1_3way_corner_pipeFat',
    '1x1_3way_corner_wire',
    '1x1_3way_hiddenWire',
    '1x1_3way_pipe',
    '1x1_3way_pipeFat',
    '1x1_3way_pipeJunction',
    '1x1_3way_wire',
    '1x1_4way_corner_pipe',
    '1x1_4way_corner_pipeFat',
    '1x1_4way_corner_pipeJunction',
    '1x1_4way_corner_wire',
    '1x1_4way_pipe',
    '1x1_4way_pipeFat',
    '1x1_4way_pipeJunction',
    '1x1_4way_wire',
    '1x1_5way_pipe',
    '1x1_5way_pipeFat',
    '1x1_5way_wire',
    '1x1_6way_pipe',
    '1x1_6way_pipeFat',
    '1x1_6way_wire',
    '1x1_basicBomb',
    '1x1_bell',
    '1x1_computer',
    '1x1_corner_hiddenWire',
    '1x1_corner_pipe',
    '1x1_corner_pipeFat',
    '1x1_corner_wire',
    '1x1_drum',
    '1x1_forbidden_frog',
    '1x1_forbidden_steeringWheel',
    '1x1_freezeBomb',
    '1x1_half_pipe',
    '1x1_hiderDetector',
    '1x1_impulseBomb',
    '1x1_lightBulb',
    '1x1_noteBlock',
    '1x1_partyButton',
    '1x1_radiationBomb',
    '1x1_screen',
    '1x1_slab2_wooden',
    '1x1_slab4_wooden',
    '1x1_smokeBomb',
    '1x1_straight_hiddenWire',
    '1x1_straight_pipe',
    '1x1_straight_pipeFat',
    '1x1_straight_wire',
    '1x1_stuffed',
    '1x1_stuffedCucumberCat',      # 131b15: picture cube, 10000 cotton
    '1x1_stuffingStorage',
    '1x1_textSign',
    '1x1_trappedWood',
    '1x1_wooden',
    '1x1_woodenHiderDetector',
    '1x1_ziplinePylon_lightsYellow',
    '1x1x2_arrowSign_forward',
    '1x1x2_arrowSign_forward45',
    '1x1x2_arrowSign_up',
    '1x1x2_arrowSign_up45',
    '1x1x2_confettiCannon',
    '1x1x2_confettiCannon_powered',
    '1x1x2_slope_cornerIn_wooden',
    '1x1x2_slope_cornerOut_wooden',
    '1x1x2_textSign',
    '1x1x2_wirelessReceiver',
    '1x1x2_wirelessTransmitter',
    '1x1x2_ziplinePylon',
    '1x1x4_raceCheckpoint',
    '1x2_beachBush',
    '1x2_bush',
    '1x2_glass',
    '1x2_icicleTrap',
    '1x2_straight_hiddenWire',
    '1x2_straight_pipe',
    '1x2_straight_pipeFat',
    '1x2_straight_wire',
    '1x2_stuffed',
    '1x2_stuffingPlant',
    '1x2_wooden',
    '1x2x1_slope_wooden',
    '1x2x2_slope_wooden',
    '1x4_stuffed',
    '1x4_wooden',
    '1x8_straight_wire',
    '1x8_wooden',
    '2x16_wooden',
    '2x2_boostPad',
    '2x2_disintegratingBlock',
    '2x2_door_grate',
    '2x2_door_hidden',
    '2x2_flowingWater',
    '2x2_glass',
    '2x2_glassBrittle',
    '2x2_ice',
    '2x2_omniBoostPad',
    '2x2_playerGate_joinLeastPopulatedTeam',
    '2x2_playerGate_joinTeam',
    '2x2_playerGate_leaveTeam',
    '2x2_playerGate_maxStuffing',
    '2x2_playerGate_pulse',
    '2x2_playerGate_revive',
    '2x2_reinforced',
    '2x2_screen',
    '2x2_slab2_wooden',
    '2x2_slab4_wooden',
    '2x2_spikes',
    '2x2_spikesFull',
    '2x2_spikes_powered',
    '2x2_springPad',
    '2x2_springPad_powered',
    '2x2_straight_hiddenWire',
    '2x2_teamBarrier',
    '2x2_trampoline',
    '2x2_trappedWood',
    '2x2_wooden',
    '2x2x1_beachRock',
    '2x2x1_grassRock',
    '2x2x1_grassRockUncovered',
    '2x2x1_sand',
    '2x2x1_slopeReverse_boostPad',
    '2x2x1_slopeReverse_flowingWater',
    '2x2x1_slope_boostPad',
    '2x2x1_slope_cornerIn_wooden',
    '2x2x1_slope_cornerOut_wooden',
    '2x2x1_slope_flowingWater',
    '2x2x1_slope_ice',
    '2x2x1_slope_wooden',
    '2x2x1_snowRock',
    '2x2x1_snowRockUncovered',
    '2x2x2_beachBush',
    '2x2x2_beachRock',
    '2x2x2_bush',
    '2x2x2_grassRock',
    '2x2x2_grassRockUncovered',
    '2x2x2_sand',
    '2x2x2_slope_boostPad',
    '2x2x2_slope_cornerIn_wooden',
    '2x2x2_slope_cornerOut_wooden',
    '2x2x2_slope_flowingWater',
    '2x2x2_slope_ice',
    '2x2x2_slope_stuffed',
    '2x2x2_slope_wooden',
    '2x2x2_snowRock',
    '2x2x2_snowRockUncovered',
    '2x2x2_stuffed',
    '2x2x2_teleporter',
    '2x2x2_teleporterDeath',
    '2x2x2_teleporterDeath_powered',
    '2x2x2_teleporterExit',
    '2x2x2_teleporterExit_powered',
    '2x2x2_teleporterRecall',
    '2x2x2_teleporter_powered',
    '2x2x2_teleporter_staff',
    '2x2x2_teleporter_team',
    '2x2x2_wooden',
    '2x4_glass',
    '2x4_glassBrittle',
    '2x4_stuffed',
    '2x4_wooden',
    '2x4x2_beachBush',
    '2x4x2_bush',
    '2x8_wooden',
    '4x2_door_hidden',
    '4x4_bouncePad',
    '4x4_door_barred',
    '4x4_door_hidden',
    '4x4_door_horizontalLines',
    '4x4_door_keyhole',
    '4x4_door_verticalLines',
    '4x4_fan',
    '4x4_glass',
    '4x4_glassBrittle',
    '4x4_playerGate_joinLeastPopulatedTeam',
    '4x4_playerGate_joinTeam',
    '4x4_playerGate_leaveTeam',
    '4x4_playerGate_maxStuffing',
    '4x4_playerGate_pulse',
    '4x4_playerGate_revive',
    '4x4_reinforced',
    '4x4_screen',
    '4x4_springPad',
    '4x4_springPad_powered',
    '4x4_stuffed',
    '4x4_teamBarrier',
    '4x4_teamBuildPlate',
    '4x4_trampoline',
    '4x4_trappedWood',
    '4x4_wooden',
    '4x4x4_beachRock',
    '4x4x4_grassRock',
    '4x4x4_grassRockUncovered',
    '4x4x4_remoteStuffingDispenser',
    '4x4x4_sand',
    '4x4x4_snowRock',
    '4x4x4_snowRockUncovered',
    '4x6_glass',
    '4x8_door_hidden',
    '4x8_door_horizontalLines',
    '4x8_door_keyhole',
    '4x8_door_verticalLines',
    '4x8_raceCheckpoint',
    '4x8_wooden',
    '4x8x4_beachBush',
    '4x8x4_bush',
    '6x6_raceCheckpoint',
    '8x8_door_hidden',
    '8x8_flowingWater',
    '8x8_glass',
    '8x8_ice',
    '8x8_spikes',
    '8x8_spikesFull',
    '8x8_spikes_powered',
    '8x8_teamBarrier',
    '8x8_teamBuildPlate',
    '8x8_trappedWood',
    '8x8_wooden',
    '8x8x4_beachRock',
    '8x8x4_grassRock',
    '8x8x4_grassRockUncovered',
    '8x8x4_sand',
    '8x8x4_snowRock',
    '8x8x4_snowRockUncovered',
    'alwaysPowering',
    'anchor',
    'balanceTeamsButton',
    'bazooka',
    'buttjo',
    'candyExplosive',
    'confettiGun',
    'copyPaster',
    'cosmeticsStore',
    'crossbow',
    'damageMultiplierSign',
    'decoration_beach_arch0',
    'decoration_beach_clam0',
    'decoration_beach_coral0',
    'decoration_beach_coral1',
    'decoration_beach_coral2',
    'decoration_beach_crab0',
    'decoration_beach_pillar0',
    'decoration_beach_pillar1',
    'decoration_beach_pillar2',
    'decoration_beach_starfish0',
    'decoration_desert_arch0',
    'decoration_desert_cactus0',
    'decoration_desert_cactus1',
    'decoration_desert_cactus2',
    'decoration_desert_cactus3',
    'decoration_desert_pillar0',
    'decoration_desert_pillar1',
    'decoration_desert_rock0',
    'decoration_desert_rock1',
    'decoration_halloween_jackOLantern0',
    'decoration_halloween_pumpkin0',
    'decoration_researchFacility_barrel',
    'decoration_researchFacility_hazmat',
    'decoration_researchFacility_radioactiveSymbol0',
    'decoration_researchFacility_radioactiveSymbol1',
    'decoration_snow_arch0',
    'decoration_snow_icicle0',
    'decoration_snow_icicle1',
    'decoration_snow_icicle2',
    'decoration_snow_pillar0',
    'decoration_snow_pillar1',
    'decoration_snow_snowman0',
    'decoration_suburb_arch0',
    'decoration_suburb_flower0',
    'decoration_suburb_flower1',
    'decoration_suburb_flower2',
    'decoration_suburb_gnome0',
    'decoration_suburb_mushroom0',
    'decoration_suburb_pillar0',
    'decoration_suburb_pillar1',
    'detonator',
    'dressingRoom',
    'endRoundEarlyButton',
    'explosiveGrenade',
    'firework',
    'flipFlop',
    'flipper',
    'footballHelmet',
    'footballHelmetUpgrade',
    'freezeGlove',
    'friendlyFireSign',
    'frisbee',
    'gameModeSign_arenaFFA',
    'gameModeSign_arenaTeams',
    'gameModeSign_hangout',
    'gameModeSign_hangoutTeams',
    'gameModeSign_hideAndSeek',
    'gameModeSign_tag',
    'grapplingHook',
    'groundLevelShifter',
    'handBell',
    'healthModeSign',
    'hotCoco',
    'impulseGrenade',
    'innerTube',
    'itemCannon',
    'itemDispenser',
    'itemRestrictionsConsole',
    'keyDispenser',
    'knightHelmet',
    'knightHelmetUpgrade',
    'letter',
    'liquidLevelShifter',
    'liquidPainter',
    'logicGate_and',
    'logicGate_not',
    'logicGate_or',
    'logicGate_xor',
    'machete',
    'mapRotator',
    'mapShifter',
    'maxStuffingCountSign',
    'microphone',
    'microphoneCursed',
    'microphonePitchDown',
    'microphonePitchUp',
    'missile',
    'ninjaHelmet',
    'ninjaHelmetUpgrade',
    'noCopySign',
    'noKnockbackProtectionSign',
    'noResetMapSign',
    'noStuffingRefundsSign',
    'noTimeLimitSign',
    'paintBrush_black',
    'paintBrush_blackhole',
    'paintBrush_blue',
    'paintBrush_blue_dark',
    'paintBrush_blue_light',
    'paintBrush_blue_turquoise',
    'paintBrush_blurple',
    'paintBrush_brown',
    'paintBrush_brown_dark',
    'paintBrush_brown_light',
    'paintBrush_cyan',
    'paintBrush_cyan_emissive',
    'paintBrush_cyan_light',
    'paintBrush_gray',
    'paintBrush_gray_dark',
    'paintBrush_green',
    'paintBrush_green_dark',
    'paintBrush_green_emissive',
    'paintBrush_green_light',
    'paintBrush_lavender',
    'paintBrush_mocha',
    'paintBrush_mocha_light',
    'paintBrush_olive',
    'paintBrush_olive_light',
    'paintBrush_orange',
    'paintBrush_orange_emissive',
    'paintBrush_orange_light',
    'paintBrush_pink',
    'paintBrush_purple',
    'paintBrush_purple_emissive',
    'paintBrush_purple_light',
    'paintBrush_red',
    'paintBrush_red_dark',
    'paintBrush_red_emissive',
    'paintBrush_teal',
    'paintBrush_white',
    'paintBrush_yellow',
    'paintBrush_yellow_emissive',
    'paintBrush_yellow_light',
    'pelletGun',
    'pelletShotgun',
    'pelletSniper',
    'pulseButton_joinTeam',
    'pulseButton_large',
    'pulseButton_leaveTeam',
    'pulseButton_small',
    'pulseButton_staff',
    'pulseButton_stuffingCost',
    'pulseButton_team',
    'pulseDelay',
    'pulseDelay_big',
    'pulseDelay_small',
    'pulseExtender',
    'pulseExtender_big',
    'pulseKeyhole',
    'raceFinish',
    'raceStart',
    'radio',
    'randomizer',
    'resetMapBlock',
    'rose',
    'shield',
    'sled',
    'smokeGrenade',
    'snowball',
    'stuffedAutoTurret',
    'stuffedCannon',
    'stuffedCopier',
    'surfboard',
    'targetDummy',
    'teamDefinitionSign',
    'teamPlayerLimitSign',
    'teamWinBlock',
    'techWeb',
    'techWeb_gameModes',
    'throwingBasicBomb',
    'throwingDecoy',
    'throwingFreezeBomb',
    'throwingImpulseBomb',
    'throwingMegaphone',
    'throwingPillowBomb',
    'throwingRadiationBomb',
    'throwingSmokeBomb',
    'throwingTeleporter',
    'umbrellaGlider',
    'woodenAutoTurret',
    'woodenCannon',
    'wristPropellor',
]

# Every 3D block/mesh item name pulled from the game's own asset names
# (yeeps_1.19.0_item_block_names.txt) -- covers the newer full-costume
# "cosmetic_<character>_<part>" system and other blocks not present in
# the earlier techWeb rotation snapshot.
ALL_ITEM_BLOCK_NAMES = [
    '16x16_glass',
    '16x16_teamBuildPlate',
    '16x16x4_desertRock',
    '16x16x4_sand',
    '16x16x4_snowRock',
    '16x16x4_snowRockUncovered',
    '16x16x8_desertRock',
    '16x16x8_snowRock',
    '1x1_3way_corner_pipe',
    '1x1_3way_corner_pipeFat2',
    '1x1_3way_corner_wireD',
    '1x1_3way_pipe',
    '1x1_3way_pipeFat2',
    '1x1_3way_pipeJunction',
    '1x1_3way_wireBlock',
    '1x1_3way_wireD',
    '1x1_4way_corner_pipe',
    '1x1_4way_corner_pipeFat2',
    '1x1_4way_corner_pipeJunction',
    '1x1_4way_corner_wireD',
    '1x1_4way_pipe',
    '1x1_4way_pipeFat2',
    '1x1_4way_pipeJunction',
    '1x1_4way_wireD',
    '1x1_5way_pipe',
    '1x1_5way_pipeFat2',
    '1x1_5way_wireD',
    '1x1_6way_pipe',
    '1x1_6way_pipeFat2',
    '1x1_6way_wireD',
    '1x1_basicBomb4',
    '1x1_basicBomb4_0',
    '1x1_bell_base',
    '1x1_bell_bell',
    '1x1_cheapStuffed',
    '1x1_coalFurnace_fueled0',
    '1x1_coalFurnace_fueled1',
    '1x1_coalFurnace_fueled2',
    '1x1_corner_pipe',
    '1x1_corner_pipeFat2',
    '1x1_corner_wireBlock',
    '1x1_corner_wireD',
    '1x1_drum',
    '1x1_forbidden_frog',
    '1x1_forbidden_steeringWheel',
    '1x1_freezeBomb4',
    '1x1_freezeBomb4_0',
    '1x1_glass',
    '1x1_half_pipe',
    '1x1_hiderDetector4',
    '1x1_hohohoBlock_base',
    '1x1_hohohoBlock_legs',
    '1x1_impulseBomb4',
    '1x1_impulseBomb4_0',
    '1x1_noteBlock',
    '1x1_noteBlock_LOD2_2',
    '1x1_partyButton_withText',
    '1x1_pumpkinBomb4',
    '1x1_pumpkinBomb4_0',
    '1x1_radiationBomb4',
    '1x1_radiationBomb4_0',
    '1x1_screamBlock_base',
    '1x1_screamBlock_head',
    '1x1_slab2_wooden',
    '1x1_slab4_wooden',
    '1x1_smokeBomb4',
    '1x1_smokeBomb4_0',
    '1x1_smokeCube',
    '1x1_straight_pipe',
    '1x1_straight_pipeFat2',
    '1x1_straight_wireBlock',
    '1x1_straight_wireD',
    '1x1_stringLightPylon',
    '1x1_stuffed2final',
    '1x1_stuffedTrophie_starCreator',
    '1x1_stuffedTrophie_superStar',
    '1x1_stuffingStorage2',
    '1x1_textSign',
    '1x1_trappedWood',
    '1x1_wiring_computer',
    '1x1_wiring_screen',
    '1x1_wooden',
    '1x1_woodenAutoTurret_base',
    '1x1_woodenHiderDetector',
    '1x1_wooden_stash',
    '1x1x2_arrowSign_forward',
    '1x1x2_arrowSign_forward45',
    '1x1x2_arrowSign_up',
    '1x1x2_arrowSign_up45',
    '1x1x2_confettiCannon',
    '1x1x2_confettiCannon_powered',
    '1x1x2_organPipe',
    '1x1x2_redEyedGnome',
    '1x1x2_redEyedGnome_hat',
    '1x1x2_slope_cornerIn_collider',
    '1x1x2_slope_cornerIn_wooden',
    '1x1x2_slope_cornerOut_collider',
    '1x1x2_slope_cornerOut_wooden',
    '1x1x2_textSign',
    '1x1x2_wirelessReceiver',
    '1x1x2_wirelessTransmitter',
    '1x1x2_ziplinePylon2',
    '1x1x4_raceCheckpoint',
    '1x2_bush',
    '1x2_doorFrame',
    '1x2_forbidden_lemon',
    '1x2_glass',
    '1x2_straight_pipe',
    '1x2_straight_pipeFat2',
    '1x2_straight_wireBlock',
    '1x2_straight_wireD',
    '1x2_stuffed2final',
    '1x2_stuffingPlant_plant2_0',
    '1x2_stuffingPlant_plant2_1',
    '1x2_stuffingPlant_plant2_2',
    '1x2_wooden',
    '1x2_wooden_stash',
    '1x2_wooden_stash2',
    '1x2x1_slope_collider',
    '1x2x1_slope_wooden',
    '1x2x2_slope_collider',
    '1x2x2_slope_wooden',
    '1x4_console',
    '1x4_doorFrame',
    '1x4_stuffed2final',
    '1x4_stuffed2final_LOD2_2',
    '1x4_wooden',
    '1x4_wooden_stash',
    '1x4x2_forbidden_textYeep',
    '1x6x2_forbidden_textError',
    '1x8_doorFrame',
    '1x8_straight_wireD',
    '1x8_wooden',
    '1x8_wooden_stash',
    '2x16_wooden',
    '2x1x3_statue_appLab',
    '2x2_boostPad',
    '2x2_door_grate',
    '2x2_flowingWater',
    '2x2_forbidden_cheese',
    '2x2_glass',
    '2x2_glassBrittle',
    '2x2_glassBrittle_shard0',
    '2x2_glassBrittle_shard1',
    '2x2_glassBrittle_shard2',
    '2x2_glassBrittle_shard3',
    '2x2_ice2',
    '2x2_omniBoostPad',
    '2x2_playerGate_joinLeastPopulatedTeam',
    '2x2_playerGate_joinTeam',
    '2x2_playerGate_leaveTeam',
    '2x2_playerGate_maxStuffing',
    '2x2_playerGate_pulse',
    '2x2_playerGate_revive',
    '2x2_prisonBars',
    '2x2_reinforced2',
    '2x2_slab2_wooden',
    '2x2_slab4_wooden',
    '2x2_spikes2',
    '2x2_spikesFull',
    '2x2_spikes_powered',
    '2x2_spikes_powered_spikes',
    '2x2_springPad',
    '2x2_springPad_launchPad',
    '2x2_springPad_powered',
    '2x2_springPad_powered_launchPad',
    '2x2_straight_wireBlock',
    '2x2_stuffed',
    '2x2_stuffedPresent',
    '2x2_stuffedPumpkin',
    '2x2_trampoline2',
    '2x2_trappedWood',
    '2x2_web',
    '2x2_wiring_screen',
    '2x2_wooden',
    '2x2x1_sand',
    '2x2x1_slopeReverse_boostPad',
    '2x2x1_slopeReverse_collider',
    '2x2x1_slopeReverse_flowingWater',
    '2x2x1_slope_boostPad',
    '2x2x1_slope_collider',
    '2x2x1_slope_cornerIn_collider',
    '2x2x1_slope_cornerIn_wooden',
    '2x2x1_slope_cornerOut_collider',
    '2x2x1_slope_cornerOut_wooden',
    '2x2x1_slope_flowingWater',
    '2x2x1_slope_ice2',
    '2x2x1_slope_wooden',
    '2x2x1_snowRock',
    '2x2x1_snowRockUncovered',
    '2x2x2_bush',
    '2x2x2_desertRock',
    '2x2x2_sand',
    '2x2x2_slope_boostPad',
    '2x2x2_slope_collider',
    '2x2x2_slope_cornerIn_collider',
    '2x2x2_slope_cornerIn_wooden',
    '2x2x2_slope_cornerOut_collider',
    '2x2x2_slope_cornerOut_wooden',
    '2x2x2_slope_flowingWater',
    '2x2x2_slope_ice2',
    '2x2x2_slope_stuffed',
    '2x2x2_slope_web',
    '2x2x2_slope_web_collider',
    '2x2x2_slope_wooden',
    '2x2x2_snowRock',
    '2x2x2_snowRockUncovered',
    '2x2x2_stuffed2final',
    '2x2x2_teleporter',
    '2x2x2_teleporterDeath',
    '2x2x2_teleporterDeath_powered_off',
    '2x2x2_teleporterDeath_powered_on',
    '2x2x2_teleporterExit',
    '2x2x2_teleporterExit_powered_off',
    '2x2x2_teleporterExit_powered_on',
    '2x2x2_teleporterRecall',
    '2x2x2_teleporter_powered_off',
    '2x2x2_teleporter_powered_on',
    '2x2x2_teleporter_staff_off',
    '2x2x2_teleporter_staff_on',
    '2x2x2_teleporter_team_off',
    '2x2x2_teleporter_team_on',
    '2x2x2_wooden',
    '2x2x4_stuffedRocketShip',
    '2x4_glass',
    '2x4_glassBrittle',
    '2x4_glassBrittle_shard0',
    '2x4_glassBrittle_shard1',
    '2x4_glassBrittle_shard2',
    '2x4_glassBrittle_shard3',
    '2x4_stuffed2final',
    '2x4_stuffed2final_0',
    '2x4_wooden',
    '2x4_wooden_stash',
    '2x4x2_bush',
    '2x8_wooden',
    '2x8_wooden_stash',
    '4x4_bouncePad2',
    '4x4_door_horizontalLines',
    '4x4_door_horizontalLinesBarred',
    '4x4_door_keyhole',
    '4x4_fan',
    '4x4_fan_blades',
    '4x4_glass',
    '4x4_glassBrittle',
    '4x4_glassBrittle_shard0',
    '4x4_glassBrittle_shard1',
    '4x4_glassBrittle_shard2',
    '4x4_glassBrittle_shard3',
    '4x4_playerGate_joinLeastPopulatedTeam',
    '4x4_playerGate_joinTeam',
    '4x4_playerGate_leaveTeam',
    '4x4_playerGate_maxStuffing',
    '4x4_playerGate_pulse',
    '4x4_playerGate_revive',
    '4x4_reinforced2',
    '4x4_springPad',
    '4x4_springPad_launchPad',
    '4x4_springPad_powered',
    '4x4_springPad_powered_launchPad',
    '4x4_stuffed2final',
    '4x4_teamBuildPlate',
    '4x4_trampoline3',
    '4x4_trampoline3_LOD2_2',
    '4x4_trappedWood',
    '4x4_web',
    '4x4_wiring_screen',
    '4x4_wooden',
    '4x4x4_remoteStuffingDispenser2',
    '4x4x4_remoteStuffingDispenser2_LOD2_2',
    '4x4x4_sand',
    '4x4x4_snowRock',
    '4x4x4_snowRockUncovered',
    '4x6_glass',
    '4x8_door_horizontalLines',
    '4x8_door_keyhole',
    '4x8_door_verticalLines',
    '4x8_raceCheckpoint2',
    '4x8_wooden',
    '4x8_wooden_stash',
    '4x8x4_bush',
    '6x6_raceCheckpoint',
    '80x80_forbidden_gyatt',
    '8x16_wooden',
    '8x8_flowingWater',
    '8x8_glass',
    '8x8_ice2',
    '8x8_spikes2',
    '8x8_spikesFull',
    '8x8_spikes_powered',
    '8x8_spikes_powered_spikes',
    '8x8_teamBuildPlate',
    '8x8_trappedWood',
    '8x8_wooden',
    '8x8x4_desertRock',
    '8x8x4_sand',
    '8x8x4_snowRock',
    '8x8x4_snowRockUncovered',
    'Brick1x2',
    'Brick2x3',
    'Combined Mesh (root_ scene) 2',
    'ComplexSphere1',
    'ComplexSphere1_TwoSided',
    'Cube1',
    'FPVGoggle_staticSphere2',
    'FPVGoggles',
    'PlushAvatar',
    'Pole4',
    'Quad_2sided',
    'RCAdvancedCar_blue_chasis',
    'RCAdvancedCar_chasis',
    'RCAdvancedCar_green_chasis',
    'RCAdvancedCar_red_chasis',
    'RCBiplane_body',
    'RCBiplane_propellor',
    'RCFighterJet_body',
    'RCFighterJet_window',
    'RCRemote',
    'RCRemote_button_ring',
    'RCRemote_button_top',
    'RCRemote_grip_ring',
    'RCRemote_grip_top',
    'RCRemote_joystick_stick',
    'RCSimpleCar_chasis',
    'RCSimpleCar_wheel',
    'RCToy_tank_barrel',
    'RCToy_tank_chasis',
    'RCToy_tank_treads',
    'RCToy_tank_turret',
    'RCToy_tank_window',
    'RCToy_train_chasis',
    'RCToy_train_smoke',
    'RCToy_train_wheel',
    'SimpleAvatar2',
    'SimpleAvatar2_Decoy',
    'SimpleAvatar2_Head',
    'SimpleAvatar2_Head_0',
    'SimpleAvatar2_LocalPlayer',
    'SimpleAvatar2_LocalPlayerNoArms',
    'SimpleAvatar2_Wooden',
    'SimpleAvatar2_Wooden_smooth',
    'SimpleAvatar2_mannequin_full',
    'SimpleAvatar2_mannequin_head',
    'SimpleAvatar2_mannequin_zipper2',
    'SimpleHand',
    'SimpleHand.001',
    'SimpleHand.002',
    'StuffingParticle0',
    'StuffingParticle1',
    'WristRCRemote',
    'adventCalendarStore',
    'anchor',
    'atm',
    'atmCurrencyDisplay',
    'atm_collider',
    'autoTurret_barrel',
    'autoTurret_legs',
    'avatarMouthTape',
    'basicTintOverlay',
    'bazooka',
    'broom',
    'broom_LOD1_old',
    'bubble',
    'bubble_0',
    'bundleConsole_collider',
    'bundleConsole_currencyPile',
    'buttar',
    'buttar_extraShard0',
    'buttar_extraShard1',
    'buttar_extraShard2',
    'buttar_shatter0',
    'buttar_shatter1',
    'buttar_shatter2',
    'buttar_shatter3',
    'buttar_shatter4',
    'buttar_shatter5',
    'buttjo',
    'buttjo_extraShard0',
    'buttjo_extraShard1',
    'buttjo_extraShard2',
    'buttjo_shatter0',
    'buttjo_shatter1',
    'buttjo_shatter2',
    'buttjo_shatter3',
    'buttjo_shatter4',
    'buttjo_shatter5',
    'buyAllConsole',
    'buyAllConsole_collider',
    'camera3',
    'cameraDrone_base2',
    'cameraDrone_blade2',
    'cameraHandle2',
    'candy_blue',
    'candy_orange',
    'candy_rainbow',
    'candy_redGreen2',
    'candy_white',
    'challengeBoard_split',
    'coal_empty',
    'coal_full',
    'communityGuidelinesConsole',
    'confettiCannon',
    'confettiGun2',
    'construction_cone2',
    'construction_sign',
    'construction_tape',
    'copyPaster',
    'corridor0',
    'corridor1',
    'corridor_void',
    'cosmetc_elf_hat',
    'cosmeticShopConsole_largeDouble',
    'cosmeticShopConsole_small',
    'cosmetic_abe_face_beard',
    'cosmetic_abe_face_eyebrows',
    'cosmetic_abe_head_hat',
    'cosmetic_alpha_arms',
    'cosmetic_alpha_body',
    'cosmetic_alpha_hat',
    'cosmetic_anglar_head',
    'cosmetic_army_arms',
    'cosmetic_army_chest_over',
    'cosmetic_army_chest_under',
    'cosmetic_army_helmet',
    'cosmetic_astronaut_arms_over',
    'cosmetic_astronaut_backpack',
    'cosmetic_astronaut_body_over',
    'cosmetic_astronaut_helmet',
    'cosmetic_backwardcap_head',
    'cosmetic_bag_head',
    'cosmetic_bakerboy_head',
    'cosmetic_bananaSplit_hat',
    'cosmetic_bananaSplit_torso',
    'cosmetic_brainJar_head',
    'cosmetic_buckethat_head',
    'cosmetic_cactus_arms',
    'cosmetic_cactus_body',
    'cosmetic_cactus_bottom',
    'cosmetic_cactus_head',
    'cosmetic_captain_head',
    'cosmetic_cat_arms_over',
    'cosmetic_cat_head',
    'cosmetic_cat_tail',
    'cosmetic_cat_torso',
    'cosmetic_chef_arms',
    'cosmetic_chef_body',
    'cosmetic_chef_bottom',
    'cosmetic_chef_head_hat',
    'cosmetic_chef_head_mustache2',
    'cosmetic_chicken_arms',
    'cosmetic_chicken_body',
    'cosmetic_chicken_bottom',
    'cosmetic_chicken_head',
    'cosmetic_christmasTree_arms_over',
    'cosmetic_christmasTree_head',
    'cosmetic_christmasTree_torso_overLayer',
    'cosmetic_clown_head',
    'cosmetic_clown_head_hair',
    'cosmetic_comedyMask',
    'cosmetic_construction_cone_head',
    'cosmetic_construction_hardHat_head',
    'cosmetic_contentCreator_mertail',
    'cosmetic_contentCreator_november2024',
    'cosmetic_contentCreator_wings',
    'cosmetic_cookedTurkey',
    'cosmetic_cow_arm',
    'cosmetic_cow_body',
    'cosmetic_cow_body_legs',
    'cosmetic_cow_head',
    'cosmetic_cowboy_arms',
    'cosmetic_cowboy_head_hat',
    'cosmetic_cowboy_mouth_cigar',
    'cosmetic_cowboy_neck',
    'cosmetic_cowboy_pants',
    'cosmetic_cowboy_torso_overLayer_vest',
    'cosmetic_cowboy_torso_underLayer_shirt',
    'cosmetic_creator_arms_overLayerSleeves',
    'cosmetic_creator_cameraPack_back',
    'cosmetic_creator_eyes_goggles',
    'cosmetic_creator_hat_santaHat',
    'cosmetic_creator_racerHelmet_head',
    'cosmetic_creator_torso_overLayer_jacket',
    'cosmetic_crown_head',
    'cosmetic_defense_head',
    'cosmetic_defense_upgrade_variant1',
    'cosmetic_devon_head',
    'cosmetic_diver_arms',
    'cosmetic_diver_body',
    'cosmetic_diver_feet',
    'cosmetic_diver_goggles_eyes',
    'cosmetic_diver_head',
    'cosmetic_diver_snorkle_mouth',
    'cosmetic_diver_tank',
    'cosmetic_doctor_arms_overLayer_jacket',
    'cosmetic_doctor_arms_underLayer_shirt',
    'cosmetic_doctor_head',
    'cosmetic_doctor_torso_overLayer_jacket',
    'cosmetic_doctor_torso_underLayer_scrubs',
    'cosmetic_dragon_arm',
    'cosmetic_dragon_body',
    'cosmetic_dragon_head',
    'cosmetic_dragon_wings',
    'cosmetic_easterBunny_arms_over',
    'cosmetic_easterBunny_head',
    'cosmetic_easterBunny_legs',
    'cosmetic_easterBunny_torso_over',
    'cosmetic_easter_bunnyEars',
    'cosmetic_elf_arms_over',
    'cosmetic_elf_body_over',
    'cosmetic_elf_body_under',
    'cosmetic_elf_ears',
    'cosmetic_elf_head_alt',
    'cosmetic_explorer_arms_over',
    'cosmetic_explorer_arms_under',
    'cosmetic_explorer_backpack',
    'cosmetic_explorer_body_under',
    'cosmetic_explorer_coat_over2',
    'cosmetic_explorer_hood',
    'cosmetic_explorer_mittens',
    'cosmetic_eyes_censored',
    'cosmetic_eyes_christmasGlasses',
    'cosmetic_eyes_circleGlasses',
    'cosmetic_eyes_memeGlasses',
    'cosmetic_eyes_monocle',
    'cosmetic_eyes_starGlasses',
    'cosmetic_facialHair_mustacheLampshade',
    'cosmetic_facialHair_mustacheMaster',
    'cosmetic_facialHair_muttonChop',
    'cosmetic_facialHair_pirateBeard',
    'cosmetic_facialHair_pirateMustache',
    'cosmetic_fancytophat_head',
    'cosmetic_farmer_beard_neck',
    'cosmetic_farmer_chickenHat_head',
    'cosmetic_farmer_head_hat',
    'cosmetic_farmer_legs_basket',
    'cosmetic_farmer_mouth_wheat',
    'cosmetic_farmer_torso_overLayer',
    'cosmetic_fireFighter_arms',
    'cosmetic_fireFighter_backpack',
    'cosmetic_fireFighter_head_hat',
    'cosmetic_fireFighter_pants',
    'cosmetic_fireFighter_torso_overLayer_jacket',
    'cosmetic_forwardcap_head',
    'cosmetic_fox_arms',
    'cosmetic_fox_body',
    'cosmetic_fox_head',
    'cosmetic_fox_tail',
    'cosmetic_frankenstein_arms_over',
    'cosmetic_frankenstein_arms_under',
    'cosmetic_frankenstein_body_under',
    'cosmetic_frankenstein_head',
    'cosmetic_frankenstein_jacket_over',
    'cosmetic_frenchman_head',
    'cosmetic_genericHair_afro',
    'cosmetic_genericHair_afro_hairPick',
    'cosmetic_genericHair_bandanaHair',
    'cosmetic_genericHair_bob',
    'cosmetic_genericHair_bobShaggy',
    'cosmetic_genericHair_bowlCut',
    'cosmetic_genericHair_boy_dreadsShort',
    'cosmetic_genericHair_boy_elvis',
    'cosmetic_genericHair_boy_fade',
    'cosmetic_genericHair_cornrows',
    'cosmetic_genericHair_curly',
    'cosmetic_genericHair_dreadsLong',
    'cosmetic_genericHair_dutchBraids',
    'cosmetic_genericHair_emo',
    'cosmetic_genericHair_flatTop',
    'cosmetic_genericHair_girlHair1',
    'cosmetic_genericHair_girlHair2',
    'cosmetic_genericHair_girlHair3',
    'cosmetic_genericHair_girlHair4',
    'cosmetic_genericHair_girlHair5',
    'cosmetic_genericHair_middlePart',
    'cosmetic_genericHair_mullet',
    'cosmetic_genericHair_receding',
    'cosmetic_genericHair_sideShave',
    'cosmetic_generic_cropTop_arms_overLayer',
    'cosmetic_generic_cropTop_torso_overLayer',
    'cosmetic_generic_dress',
    'cosmetic_generic_dressPants',
    'cosmetic_generic_hoodie_arms_overLayer',
    'cosmetic_generic_hoodie_torso_underLayer',
    'cosmetic_generic_longSleeve_torso_underLayer',
    'cosmetic_generic_longSleeves_arms_underLayer',
    'cosmetic_generic_polo_arms_overLayer',
    'cosmetic_generic_polo_torso_underLayer',
    'cosmetic_generic_puffedCrop_torso_overLayer',
    'cosmetic_generic_puffedCropped_arms_overLayer',
    'cosmetic_generic_skirt',
    'cosmetic_generic_sweatPants',
    'cosmetic_generic_sweater_arms_overLayer',
    'cosmetic_generic_sweater_torso_overLayer',
    'cosmetic_generic_tShirt_arms_overLayer',
    'cosmetic_generic_tShirt_torso_underLayer',
    'cosmetic_generic_tankTop_torso_underLayer',
    'cosmetic_giraffe_arms',
    'cosmetic_giraffe_body',
    'cosmetic_giraffe_bottom',
    'cosmetic_giraffe_head2',
    'cosmetic_gogConquest_head',
    'cosmetic_gogDefault_head',
    'cosmetic_gogDefense_head',
    'cosmetic_gogDestruction_head',
    'cosmetic_gogProduction_head',
    'cosmetic_gogStealth_head',
    'cosmetic_gogTeleportation_head',
    'cosmetic_gogVolatility_head',
    'cosmetic_gog_defaultHead',
    'cosmetic_gog_havoc_head2',
    'cosmetic_gorilla_arms',
    'cosmetic_gorilla_head',
    'cosmetic_gorilla_pants_legs',
    'cosmetic_gorilla_torso',
    'cosmetic_grad_arms_over',
    'cosmetic_grad_back_backpack',
    'cosmetic_grad_head_cap',
    'cosmetic_grad_torso_overLayer_gown',
    'cosmetic_groucho_head_glasses',
    'cosmetic_hat_blobFish',
    'cosmetic_hat_candle',
    'cosmetic_hat_cat',
    'cosmetic_hat_catEyes',
    'cosmetic_hat_cheese',
    'cosmetic_hat_christmasWreath',
    'cosmetic_hat_cupPong',
    'cosmetic_hat_devilHorns',
    'cosmetic_hat_earMuffs',
    'cosmetic_hat_fuzzHat',
    'cosmetic_hat_halo',
    'cosmetic_hat_horse',
    'cosmetic_hat_iceSkate',
    'cosmetic_hat_jester',
    'cosmetic_hat_kelpCrown',
    'cosmetic_hat_laurel',
    'cosmetic_hat_medusa',
    'cosmetic_hat_mushroom',
    'cosmetic_hat_neckTie',
    'cosmetic_hat_octopus',
    'cosmetic_hat_pancake',
    'cosmetic_hat_party',
    'cosmetic_hat_penguin',
    'cosmetic_hat_pirateBandana',
    'cosmetic_hat_pizzaBox',
    'cosmetic_hat_scarecrow',
    'cosmetic_hat_seashell',
    'cosmetic_hat_sharkBite',
    'cosmetic_hat_sharkFin',
    'cosmetic_hat_snowBeanie',
    'cosmetic_hat_snowHelmet',
    'cosmetic_hat_snowmanHat',
    'cosmetic_hat_snowmanMelting',
    'cosmetic_hat_soldierHelmet',
    'cosmetic_hat_spider',
    'cosmetic_hat_takeoutBox',
    'cosmetic_hat_tropicalFlower',
    'cosmetic_hat_wizard',
    'cosmetic_hawaiianDrink_bottom',
    'cosmetic_hawaiianDrink_hat',
    'cosmetic_hawaiianShirt_arms',
    'cosmetic_hawaiianShirt_over',
    'cosmetic_hazmat_arms_overLayer',
    'cosmetic_hazmat_back_canister',
    'cosmetic_hazmat_head',
    'cosmetic_hazmat_torso_overLayer',
    'cosmetic_head_facePaint',
    'cosmetic_head_hood',
    'cosmetic_head_scar',
    'cosmetic_head_whiteFlag',
    'cosmetic_headphones3_head',
    'cosmetic_hipster_arms_over',
    'cosmetic_hipster_arms_under',
    'cosmetic_hipster_chest_over',
    'cosmetic_hipster_chest_under',
    'cosmetic_hipster_head_glasses',
    'cosmetic_hipster_head_hat',
    'cosmetic_hockeyMask_head',
    'cosmetic_hockey_arm',
    'cosmetic_hockey_body',
    'cosmetic_hockey_bottom',
    'cosmetic_hockey_head',
    'cosmetic_hockey_tail',
    'cosmetic_jackOLantern_arms_overLayer',
    'cosmetic_jackOLantern_arms_underLayer',
    'cosmetic_jackOLantern_head',
    'cosmetic_jackOLantern_neck_collar',
    'cosmetic_jackOLantern_torso_overLayer',
    'cosmetic_jackOLantern_torso_underLayer',
    'cosmetic_juicecap_head',
    'cosmetic_kittyEars_head',
    'cosmetic_lemon_head',
    'cosmetic_lifeguard_arms_over_floaties',
    'cosmetic_lifeguard_back_floaty',
    'cosmetic_lifeguard_eyes_sunglasses',
    'cosmetic_lifeguard_hat_visor',
    'cosmetic_lifeguard_mouth_whistle',
    'cosmetic_lifeguard_nose',
    'cosmetic_lifeguard_pants',
    'cosmetic_lifeguard_torso_underLayer',
    'cosmetic_lion_head',
    'cosmetic_merYeep_head_hair',
    'cosmetic_merYeep_pants_tail',
    'cosmetic_merYeep_torso_seashells_overLayer',
    'cosmetic_mobility_head',
    'cosmetic_mobility_head_upgrade',
    'cosmetic_monster_arms',
    'cosmetic_monster_body_over',
    'cosmetic_monster_head',
    'cosmetic_mrsClause_arms_over',
    'cosmetic_mrsClause_body_over',
    'cosmetic_mrsClause_eyes',
    'cosmetic_mrsClause_head',
    'cosmetic_mummy_head',
    'cosmetic_neck_bandana',
    'cosmetic_neck_headPhones',
    'cosmetic_neck_scarfWrapped',
    'cosmetic_need4speed_head',
    'cosmetic_nose_carrotNose',
    'cosmetic_nutcracker_arms_over',
    'cosmetic_nutcracker_body_over',
    'cosmetic_nutcracker_hat',
    'cosmetic_nutcracker_lipLower',
    'cosmetic_nutcracker_lipUpper',
    'cosmetic_offense_head',
    'cosmetic_offense_head_upgrade',
    'cosmetic_pajama_arms_over',
    'cosmetic_pajama_back_teddyBear',
    'cosmetic_pajama_eyes_eyeMask',
    'cosmetic_pajama_hat',
    'cosmetic_pajama_pants',
    'cosmetic_pajama_torso_over',
    'cosmetic_pirate_arms_overLayer',
    'cosmetic_pirate_coat_overLayer',
    'cosmetic_pirate_face_eyePatch',
    'cosmetic_pirate_feet',
    'cosmetic_pirate_hat',
    'cosmetic_pirate_headAlt',
    'cosmetic_pirate_headAlt_eyes',
    'cosmetic_pirate_mouth_sword',
    'cosmetic_pirate_pants',
    'cosmetic_pirate_shirt_underLayer',
    'cosmetic_plant_head',
    'cosmetic_polarBear_arm_over',
    'cosmetic_polarBear_feet',
    'cosmetic_polarBear_head',
    'cosmetic_polarBear_tail',
    'cosmetic_polarBear_torso_over',
    'cosmetic_policeFull_arms',
    'cosmetic_policeFull_head_hat',
    'cosmetic_policeFull_pants',
    'cosmetic_policeFull_torso_overLayer_shirt',
    'cosmetic_policeFull_wrist_watch',
    'cosmetic_police_eyes_glasses',
    'cosmetic_police_head_hat',
    'cosmetic_police_head_siren',
    'cosmetic_pot_head',
    'cosmetic_presentAlt_head',
    'cosmetic_present_head',
    'cosmetic_pug_head',
    'cosmetic_pug_torso_over',
    'cosmetic_pug_zipper',
    'cosmetic_rat_arms_under',
    'cosmetic_rat_body_under',
    'cosmetic_rat_head',
    'cosmetic_rat_tail',
    'cosmetic_reaper_arms',
    'cosmetic_reaper_chest_over',
    'cosmetic_reaper_chest_under',
    'cosmetic_reaper_hood',
    'cosmetic_reaper_mask',
    'cosmetic_reaper_staff',
    'cosmetic_redRidingHood_arms',
    'cosmetic_redRidingHood_back',
    'cosmetic_redRidingHood_face_freckles',
    'cosmetic_redRidingHood_head_hood',
    'cosmetic_redRidingHood_torso_over',
    'cosmetic_reindeer_arms_over',
    'cosmetic_reindeer_head',
    'cosmetic_reindeer_head_alt',
    'cosmetic_reindeer_torso_legs',
    'cosmetic_reindeer_torso_over',
    'cosmetic_robber_head',
    'cosmetic_robot_arms',
    'cosmetic_robot_chest',
    'cosmetic_robot_feet',
    'cosmetic_robot_head',
    'cosmetic_rockStar_arm_over',
    'cosmetic_rockStar_eyes_makeup',
    'cosmetic_rockStar_head_mohawk',
    'cosmetic_rockStar_mouth_piercing',
    'cosmetic_rockStar_neck_choker',
    'cosmetic_rockStar_pants',
    'cosmetic_rockStar_torso_over',
    'cosmetic_samurai_arms',
    'cosmetic_samurai_bottom',
    'cosmetic_samurai_chest',
    'cosmetic_samurai_eyes_glasses',
    'cosmetic_samurai_hat',
    'cosmetic_samurai_head',
    'cosmetic_samurai_head_onimask',
    'cosmetic_samurai_tail',
    'cosmetic_santa_arms_overLayer',
    'cosmetic_santa_back',
    'cosmetic_santa_facialHair_beard',
    'cosmetic_santa_facialHair_mustache',
    'cosmetic_santa_head_glasses',
    'cosmetic_santa_head_hair',
    'cosmetic_santa_torso_overLayer',
    'cosmetic_santa_torso_underLayer',
    'cosmetic_seaGrimReaper_arms_over',
    'cosmetic_seaGrimReaper_head',
    'cosmetic_seaGrimReaper_head_mask',
    'cosmetic_seaGrimReaper_torso_over',
    'cosmetic_sergeant_arms_overLayer',
    'cosmetic_sergeant_hat',
    'cosmetic_sergeant_pants',
    'cosmetic_sergeant_torso_overLayer',
    'cosmetic_sergeant_torso_underLayer',
    'cosmetic_shark_arms',
    'cosmetic_shark_body',
    'cosmetic_shark_hammerHead',
    'cosmetic_shark_head',
    'cosmetic_shark_tail',
    'cosmetic_sigma_arms',
    'cosmetic_sigma_body_over',
    'cosmetic_sigma_bottoms',
    'cosmetic_sigma_chin',
    'cosmetic_sigma_hair',
    'cosmetic_sigma_head',
    'cosmetic_skibidi_bottom_over',
    'cosmetic_skibidi_head',
    'cosmetic_skibidi_tail',
    'cosmetic_skier_arms',
    'cosmetic_skier_body_over',
    'cosmetic_skier_bottom',
    'cosmetic_skier_bottom_feet',
    'cosmetic_skier_head_goggles_front',
    'cosmetic_skier_head_hood',
    'cosmetic_skier_tail',
    'cosmetic_snowBoarder_arms',
    'cosmetic_snowBoarder_back_snowboard',
    'cosmetic_snowBoarder_eyes_goggles',
    'cosmetic_snowBoarder_head_beanie',
    'cosmetic_snowBoarder_legs',
    'cosmetic_snowBoarder_torso_over',
    'cosmetic_snowFlannel_head',
    'cosmetic_snowHatgreen_head',
    'cosmetic_snowman_body',
    'cosmetic_snowman_head',
    'cosmetic_snowman_neck',
    'cosmetic_spartan_arms_over',
    'cosmetic_spartan_head_alt',
    'cosmetic_spartan_head_helmet',
    'cosmetic_spartan_pants',
    'cosmetic_spartan_torso_over',
    'cosmetic_squid_arms_over',
    'cosmetic_squid_head',
    'cosmetic_squid_torso_over',
    'cosmetic_straightJacket_arm',
    'cosmetic_straightJacket_eyes_eyeLiner',
    'cosmetic_straightJacket_head_hair',
    'cosmetic_straightJacket_torso_over',
    'cosmetic_submariner_arms',
    'cosmetic_submariner_body',
    'cosmetic_submariner_head',
    'cosmetic_suit_arms_overLayer',
    'cosmetic_suit_arms_underLayer',
    'cosmetic_suit_torso_overLayer',
    'cosmetic_suit_torso_underLayer',
    'cosmetic_sunflower_head',
    'cosmetic_thanksGiving_gobble_head',
    'cosmetic_thief_arms',
    'cosmetic_thief_body',
    'cosmetic_thief_head_eyes',
    'cosmetic_thief_head_hat',
    'cosmetic_thief_tail',
    'cosmetic_turkey_arms_overLayer',
    'cosmetic_turkey_head',
    'cosmetic_turkey_pants',
    'cosmetic_turkey_tail',
    'cosmetic_turkey_torso_overLayer',
    'cosmetic_tuxedo_bottom_over',
    'cosmetic_tuxedo_hat',
    'cosmetic_tuxedo_jacket_over',
    'cosmetic_tuxedo_shirt_under',
    'cosmetic_tuxedo_sleeves_over',
    'cosmetic_tuxedo_sleeves_under',
    'cosmetic_vampire_arms_over',
    'cosmetic_vampire_body_over',
    'cosmetic_vampire_body_under',
    'cosmetic_vampire_hair',
    'cosmetic_vampire_neck',
    'cosmetic_vampire_pants_Over',
    'cosmetic_vampire_teeth',
    'cosmetic_voodoo_arms_over',
    'cosmetic_voodoo_arms_under',
    'cosmetic_voodoo_backpack',
    'cosmetic_voodoo_body_over',
    'cosmetic_voodoo_body_under',
    'cosmetic_voodoo_hat',
    'cosmetic_voodoo_mask',
    'cosmetic_werewolf_head',
    'cosmetic_winterreindeer_head',
    'cosmetic_witch_arms_overLayer',
    'cosmetic_witch_arms_underLayer',
    'cosmetic_witch_head',
    'cosmetic_witch_pants_cauldron_over',
    'cosmetic_witch_torso_overLayer',
    'cosmetic_witch_torso_underLayer',
    'cosmetic_wizard_arms_outter',
    'cosmetic_wizard_beard',
    'cosmetic_wizard_body_over',
    'cosmetic_wizard_body_under',
    'cosmetic_wizard_hat',
    'cosmetic_wizard_wand',
    'cosmetic_wolfGrandma_arms_over',
    'cosmetic_wolfGrandma_head',
    'cosmetic_wolfGrandma_torso_over',
    'cosmetic_wolfGrandma_torso_tail',
    'cosmetic_yambo_arms_over',
    'cosmetic_yambo_body_over',
    'cosmetic_yambo_body_under',
    'cosmetic_yambo_head',
    'cosmetic_yambo_pants_over',
    'cosmetic_yeti_arms_overLayer',
    'cosmetic_yeti_head',
    'cosmetic_yeti_torso_overLayer',
    'cosmetic_zipperSnow_babyPenguin',
    'cosmetic_zipperSnow_fireplace',
    'cosmetic_zipperSnow_polarBear',
    'cosmetic_zipperSnow_snowman',
    'cosmetic_zipper_100k',
    'cosmetic_zipper_anchor',
    'cosmetic_zipper_appLab',
    'cosmetic_zipper_arrow',
    'cosmetic_zipper_atom',
    'cosmetic_zipper_avatar',
    'cosmetic_zipper_badge_LabRat',
    'cosmetic_zipper_badge_creatorStar',
    'cosmetic_zipper_badge_moderator',
    'cosmetic_zipper_badge_superStar',
    'cosmetic_zipper_badge_trassDev',
    'cosmetic_zipper_bat',
    'cosmetic_zipper_bell',
    'cosmetic_zipper_binoculars',
    'cosmetic_zipper_boba',
    'cosmetic_zipper_bow_chain',
    'cosmetic_zipper_boxingGlove',
    'cosmetic_zipper_bus',
    'cosmetic_zipper_camera',
    'cosmetic_zipper_candy',
    'cosmetic_zipper_candyCane',
    'cosmetic_zipper_candyCorn',
    'cosmetic_zipper_casket',
    'cosmetic_zipper_cheese',
    'cosmetic_zipper_cherry',
    'cosmetic_zipper_chicken',
    'cosmetic_zipper_clock',
    'cosmetic_zipper_coinned',
    'cosmetic_zipper_column',
    'cosmetic_zipper_croc',
    'cosmetic_zipper_dagger',
    'cosmetic_zipper_default',
    'cosmetic_zipper_dice',
    'cosmetic_zipper_die',
    'cosmetic_zipper_donut',
    'cosmetic_zipper_easterEgg',
    'cosmetic_zipper_fireCan',
    'cosmetic_zipper_fishBones',
    'cosmetic_zipper_frap',
    'cosmetic_zipper_gingerBread',
    'cosmetic_zipper_gogShip',
    'cosmetic_zipper_goldToilet',
    'cosmetic_zipper_grenade',
    'cosmetic_zipper_grill',
    'cosmetic_zipper_gummieBear',
    'cosmetic_zipper_hamburger',
    'cosmetic_zipper_heartCharm',
    'cosmetic_zipper_hotDog',
    'cosmetic_zipper_iceCream',
    'cosmetic_zipper_knife',
    'cosmetic_zipper_koi',
    'cosmetic_zipper_lightningBolt',
    'cosmetic_zipper_loginStreakBadge_bronze',
    'cosmetic_zipper_loginStreakBadge_diamond',
    'cosmetic_zipper_loginStreakBadge_gold',
    'cosmetic_zipper_loginStreakBadge_platinum',
    'cosmetic_zipper_loginStreakBadge_silver',
    'cosmetic_zipper_macaron',
    'cosmetic_zipper_medal',
    'cosmetic_zipper_monsterHead',
    'cosmetic_zipper_music',
    'cosmetic_zipper_neckTie',
    'cosmetic_zipper_onion',
    'cosmetic_zipper_ornament',
    'cosmetic_zipper_owl',
    'cosmetic_zipper_phone',
    'cosmetic_zipper_pillow',
    'cosmetic_zipper_popsicle',
    'cosmetic_zipper_present',
    'cosmetic_zipper_puffer',
    'cosmetic_zipper_pumpkin',
    'cosmetic_zipper_rainbow',
    'cosmetic_zipper_rocket',
    'cosmetic_zipper_runnyNose',
    'cosmetic_zipper_sandBucket',
    'cosmetic_zipper_seashell',
    'cosmetic_zipper_sharkTooth',
    'cosmetic_zipper_shoe',
    'cosmetic_zipper_shrimp',
    'cosmetic_zipper_skateBoard',
    'cosmetic_zipper_skull',
    'cosmetic_zipper_snowFlake',
    'cosmetic_zipper_spider',
    'cosmetic_zipper_stocking',
    'cosmetic_zipper_swipeNshoot',
    'cosmetic_zipper_teeth',
    'cosmetic_zipper_theJeff',
    'cosmetic_zipper_thermometer',
    'cosmetic_zipper_tombstone',
    'cosmetic_zipper_trojanHorse',
    'cosmetic_zipper_tropicalFlower',
    'cosmetic_zipper_voodooHead',
    'cosmetic_zipper_winterMask',
    'cosmetic_zipper_yeepSkull',
    'cosmetic_zipper_yeepTags',
    'cosmeticsStore2',
    'crossbow_fired',
    'crossbow_loaded',
    'currencyButton_Baked',
    'damageMultiplierSign_0.5x',
    'damageMultiplierSign_0x',
    'damageMultiplierSign_1.5x',
    'damageMultiplierSign_2x',
    'damageMultiplierSign_4x',
    'damageMultiplierSign_8x',
    'decoration_ beach_hangingSignBig',
    'decoration_beach_arch0',
    'decoration_beach_clam',
    'decoration_beach_clam_stashBase',
    'decoration_beach_clam_stashContainer',
    'decoration_beach_coral0',
    'decoration_beach_coral1',
    'decoration_beach_coral2',
    'decoration_beach_crab',
    'decoration_beach_crab_stashBase',
    'decoration_beach_crab_stashContainer',
    'decoration_beach_gnome3',
    'decoration_beach_gnome3_stashBase',
    'decoration_beach_gnome3_stashContainer',
    'decoration_beach_hangingSign',
    'decoration_beach_pillar0',
    'decoration_beach_pillar1',
    'decoration_beach_pillar2',
    'decoration_beach_starfish0',
    'decoration_christmas_gnome0',
    'decoration_christmas_gnome1',
    'decoration_christmas_gnome2',
    'decoration_christmas_ornament0',
    'decoration_christmas_ornament1',
    'decoration_christmas_ornament2',
    'decoration_christmas_reindeer',
    'decoration_christmas_wreath',
    'decoration_desert_arch0',
    'decoration_desert_cactus0',
    'decoration_desert_cactus1',
    'decoration_desert_cactus2',
    'decoration_desert_cactus3',
    'decoration_desert_cactus4',
    'decoration_desert_pillar0',
    'decoration_desert_pillar1',
    'decoration_desert_rock0',
    'decoration_desert_rock1',
    'decoration_easterEgg_stashBase',
    'decoration_easterEgg_stashContainer',
    'decoration_goingAwaySign4',
    'decoration_halloween_gnome0',
    'decoration_halloween_gnome1',
    'decoration_halloween_gnome2',
    'decoration_halloween_gnome3',
    'decoration_halloween_gnome4',
    'decoration_halloween_jackOLantern',
    'decoration_halloween_pumpkin',
    'decoration_halloween_spider',
    'decoration_halloween_tombstone0',
    'decoration_present_stashBase',
    'decoration_present_stashContainer',
    'decoration_researchFacility_barrel',
    'decoration_researchFacility_hangingSign',
    'decoration_researchFacility_hangingSignBig',
    'decoration_researchFacility_hazmat0',
    'decoration_researchFacility_radioactiveSymbol0',
    'decoration_researchFacility_radioactiveSymbol1',
    'decoration_snow_arch0',
    'decoration_snow_hangingSign',
    'decoration_snow_hangingSignBig',
    'decoration_snow_icicle0',
    'decoration_snow_icicle1',
    'decoration_snow_icicle2',
    'decoration_snow_pillar0',
    'decoration_snow_pillar1',
    'decoration_snow_snowman0',
    'decoration_snow_snowman0_stashBase',
    'decoration_snow_snowman0_stashContainer',
    'decoration_suburb_arch0',
    'decoration_suburb_chest_stashBase',
    'decoration_suburb_chest_stashContainer',
    'decoration_suburb_flower0',
    'decoration_suburb_flower0_stashBase',
    'decoration_suburb_flower0_stashContainer0',
    'decoration_suburb_flower0_stashContainer1',
    'decoration_suburb_flower0_stashContainer2',
    'decoration_suburb_flower0_stashContainer3',
    'decoration_suburb_flower1',
    'decoration_suburb_flower1_stashBase',
    'decoration_suburb_flower1_stashContainer',
    'decoration_suburb_flower2',
    'decoration_suburb_flower2_stashBase',
    'decoration_suburb_flower2_stashContainer',
    'decoration_suburb_gnome0',
    'decoration_suburb_gnome2',
    'decoration_suburb_gnome2_stashBase',
    'decoration_suburb_gnome2_stashContainer',
    'decoration_suburb_gnome_stashBase',
    'decoration_suburb_gnome_stashContainer',
    'decoration_suburb_hangingSign',
    'decoration_suburb_hangingSignBig',
    'decoration_suburb_hazmat0',
    'decoration_suburb_mushroom0',
    'decoration_suburb_mushroom0_stashBase',
    'decoration_suburb_mushroom0_stashContainer',
    'decoration_suburb_pillar0',
    'decoration_suburb_pillar0_stashBase',
    'decoration_suburb_pillar0_stashContainer',
    'decoration_suburb_pillar1',
    'detonator4',
    'discoBall',
    'disintegratingBlock2',
    'dressingRoom',
    'endRoundEarlyButton',
    'enderpearl3',
    'entryWay_backrooms_8x12',
    'entryWay_beach_4x4',
    'entryWay_beach_8x12',
    'entryWay_researchFacility2_8x12',
    'entryWay_researchFacility_4x4',
    'entryWay_snow_4x4',
    'entryWay_snow_8x12',
    'entryWay_suburb_4x4',
    'entryWay_suburb_8x12',
    'explorerSatelite_base',
    'explorerSatelite_base_LOD2_fixed',
    'explorerSatellite_dish',
    'firework2',
    'flipper',
    'footprintParticle',
    'freezeGlove2',
    'friendlyFireSign',
    'frisbee',
    'frozenOverlay',
    'gameModeSign_arenaFFA',
    'gameModeSign_arenaTeams',
    'gameModeSign_hangout3',
    'gameModeSign_hangoutTeams',
    'gameModeSign_hideAndSeek',
    'gameModeSign_monster',
    'gameModeSign_tag',
    'geiger2',
    'geigerNeedle',
    'giftBox_closed',
    'giftBox_open',
    'giftBox_particle0',
    'giftBox_particle1',
    'giftBox_particle2',
    'grapplingHook2_base',
    'grapplingHook2_hook',
    'grapplingHook2_hookTop',
    'grapplingHook_base_spider',
    'grapplingHook_hook_spider',
    'grenade_explosive',
    'grenade_explosivePumpkin',
    'grenade_explosive_frag',
    'grenade_freeze',
    'grenade_impulse',
    'grenade_pillow',
    'grenade_radiation',
    'grenade_smoke',
    'groundLevelShifter',
    'halloweenStore',
    'handBell',
    'headReverseCamera4',
    'headReverseCamera4_wornHide',
    'healthModeSign',
    'healthParticle',
    'heart',
    'hotCocoParticle0',
    'hotCocoParticle1',
    'hotCoco_cup',
    'hotCoco_fill',
    'icicleTrap',
    'icicle_shatterParticle_0',
    'icicle_shatterParticle_1',
    'icicle_shatterParticle_2',
    'icicle_shatterParticle_3',
    'innerTube',
    'innerTube_duck',
    'itemRestrictionsConsole2',
    'itemRestrictionsConsole2_input',
    'joinTeamButton',
    'key',
    'leaveTeamButton',
    'letter_closed',
    'letter_open',
    'liquidLevelShifter2',
    'liquidPainter2',
    'lobby0',
    'lobby0_doorBlock_0',
    'lobby0_doorBlock_1',
    'lobby0_doorBlock_2',
    'machete4',
    'machete_arena',
    'machete_pirateSword',
    'machete_samurai',
    'magnifyingGlass2',
    'mapManagement_suburb',
    'mapObjectPreview',
    'mapShifter',
    'maxStuffingSign_1000',
    'maxStuffingSign_10000',
    'maxStuffingSign_2000',
    'maxStuffingSign_3000',
    'maxStuffingSign_500',
    'maxStuffingSign_50000',
    'maxStuffingSign_7000',
    'merchStore',
    'microphone',
    'microphone_cursed',
    'microphone_pitchDown',
    'microphone_pitchUp',
    'mirrorAvatarStand_low',
    'missile',
    'missile_gogNuke',
    'musicNote0',
    'musicNote0_electric',
    'musicNote1',
    'musicNote1_electric',
    'noCopySign',
    'noKnockbackProtectionSign',
    'noResetMapSign',
    'noStuffingRefundsSign',
    'noTimeLimitSign',
    'ownedCosmeticsDisplay',
    'paintBrush3',
    'pelletGun3',
    'pelletGun_army',
    'pelletGun_pirate2',
    'pelletShotgun',
    'pelletSniper',
    'playerList2_beach',
    'playerList2_snow',
    'playerList2_suburb',
    'playerList_desert',
    'plushBoxes',
    'portalConsole',
    'portalConsole_communityWorlds',
    'portalConsole_small',
    'portalEffect',
    'priceTag3',
    'priceTag3_wide',
    'projectile_cannonball',
    'projectile_pellet',
    'projectile_shotgunPellet',
    'projectile_sniperPellet',
    'promoCodeConsole2',
    'propellor2',
    'propellor_blade',
    'pushButtonCover',
    'pushButton_arrow',
    'pushButton_arrowBar2',
    'pushButton_arrowBar2_shell',
    'pushButton_arrow_shell',
    'pushButton_currencyStash',
    'pushButton_currencyStash_crab',
    'pushButton_currencyStash_flowerCenter',
    'pushButton_currencyStash_gnomeNose',
    'pushButton_currencyStash_snowmanNose',
    'pushButton_currencyStash_stem',
    'pushButton_flip',
    'pushButton_flip_shell',
    'pushButton_large',
    'pushButton_largeCircle',
    'pushButton_large_shell',
    'pushButton_party',
    'pushButton_party_shell',
    'pushButton_pause',
    'pushButton_play',
    'pushButton_play_shell',
    'pushButton_rotate',
    'pushButton_rotate180',
    'pushButton_rotate180_shell',
    'pushButton_rotate_shell',
    'pushButton_small',
    'pushButton_small_green',
    'pushButton_small_minus',
    'pushButton_small_plus',
    'pushButton_small_white',
    'pushButton_small_wide',
    'pushButton_stuffedCannon',
    'raceCheckpoint_check',
    'raceCheckpoint_check_4sided',
    'raceFinish',
    'raceStart_base',
    'raceStart_gate',
    'raceStart_lightOn',
    'radio',
    'rareItemStore',
    'rebalanceTeamsButton',
    'rose',
    'rose_shatter0',
    'rose_shatter1',
    'rose_shatter2',
    'sceneryBeach_anchor0',
    'sceneryBeach_barrel0',
    'sceneryBeach_rock0',
    'sceneryBeach_rock1',
    'sceneryBeach_rock2',
    'sceneryBeach_rock3',
    'sceneryBeach_rock4',
    'sceneryBeach_rock5',
    'sceneryBeach_ship0',
    'sceneryBeach_ship1',
    'sceneryBeach_ship2',
    'sceneryBeach_wheel0',
    'scenerySkybox',
    'scenerySkybox_noGroundRising',
    'scenerySnow_fence0',
    'scenerySnow_fence1',
    'scenerySnow_igloo0',
    'scenerySnow_igloo1',
    'scenerySnow_igloo2',
    'scenerySnow_log0',
    'scenerySnow_moon0',
    'scenerySnow_mountain0',
    'scenerySnow_mountain1',
    'scenerySnow_rock0',
    'scenerySnow_rock1',
    'scenerySnow_tree0',
    'scenerySnow_tree1',
    'scenerySuburb_gate0',
    'scenerySuburb_house0',
    'scenerySuburb_house1',
    'scenerySuburb_house2',
    'scenerySuburb_light0',
    'scenerySuburb_light1',
    'scenerySuburb_light2',
    'scenerySuburb_moon0',
    'scenerySuburb_plant0',
    'scenerySuburb_tree0',
    'scenerySuburb_tree1',
    'selfieStick2',
    'shield',
    'signPost',
    'signPost_sign',
    'signPost_sign_player',
    'skullParticle_generic0',
    'skullParticle_generic1',
    'skullParticle_generic2',
    'skullParticle_generic3',
    'skullParticle_shatter0',
    'skullParticle_shatter3',
    'sled3',
    'snowMachine_barrel',
    'snowMachine_base',
    'snowball3',
    'snowball_skull',
    'snowflakeParticle0',
    'sparkler',
    'stringLight',
    'stuffedCannon_barrel',
    'stuffedCannon_base',
    'stuffedCopier2',
    'stuffingDispenser2_collider',
    'stuffingDispenser3',
    'suburb_stars_less',
    'surfboard',
    'symbol_Carrot',
    'symbol_X',
    'symbol_arrow',
    'symbol_check',
    'targetDummyStand',
    'teamDefinitionSign',
    'teamLimitSign_1',
    'teamLimitSign_2',
    'teamLimitSign_3',
    'teamLimitSign_4',
    'teamLimitSign_8',
    'techWeb',
    'techWebLine_dotted_long',
    'techWebLine_dotted_long2',
    'techWebLine_solid',
    'techWeb_board',
    'techWeb_board_collider',
    'techWeb_board_collider_narrow',
    'techWeb_gameModes',
    'throwingBasicBomb2',
    'throwingFreezeBomb2',
    'throwingImpulseBomb2',
    'throwingPillowBomb2',
    'throwingPin',
    'throwingPin_outline',
    'throwingPlunger',
    'throwingPumpkinBomb2',
    'throwingRadiationBomb2',
    'throwingSmokeBomb2',
    'throwingSnowflake',
    'toyLoader',
    'toyStore',
    'transitionPortal',
    'trophyBAM',
    'umbrellaHandle3',
    'umbrellaTop2',
    'windupBomb_base',
    'windupBomb_foot',
    'windupBomb_spin',
    'winterWishlistConsole',
    'winterWishlistDrop_chute',
    'winterWishlistDrop_tube',
    'wiring_alwaysOn',
    'wiring_delay_long',
    'wiring_delay_short',
    'wiring_delay_tiny',
    'wiring_flipFlop',
    'wiring_gate_and',
    'wiring_gate_not',
    'wiring_gate_or',
    'wiring_gate_xor',
    'wiring_itemCannon_barrel',
    'wiring_itemCannon_base',
    'wiring_itemDispenser',
    'wiring_keyDispenser',
    'wiring_keyhole',
    'wiring_lightBulb3',
    'wiring_pulseButton',
    'wiring_pulseButton_staff',
    'wiring_pulseButton_stuffing100',
    'wiring_pulseButton_stuffing1000',
    'wiring_pulseButton_stuffing250',
    'wiring_pulseButton_stuffing2500',
    'wiring_pulseButton_stuffing500',
    'wiring_pulseButton_stuffing5000',
    'wiring_pulseButton_team',
    'wiring_pulseExtendor_long',
    'wiring_pulseExtendor_short',
    'wiring_randomizer',
    'wiring_resetMap',
    'wiring_teamWin',
    'wishlistScroll',
    'witchesBroom_particle0',
    'witchesBroom_particle1',
    'witchesBroom_particle2',
    'woodCurrencyStashFlap',
    'woodenAutoTurret_turret',
    'woodenCannon_barrel',
    'woodenCannon_base',
    'worldFuelConsole',
    'worldFuelConsole_collider',
    'worldFuelConsole_fuel',
    'worldSettings_beach',
    'worldSettings_snow',
    'worldSettings_suburb',
    'wrapper_blue',
    'wrapper_orange',
    'wrapper_rainbow',
    'wrapper_redGreen2',
    'wrapper_white',
    'yetiGlove',
    'ziplineCord',
    'zipper_wishlistScroll',
]


def _server_time() -> str:
    """Current time in the game's format: 'YYYY MM/DD HH:mm:ss'"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y %m/%d %H:%M:%S")


def _random_session_id() -> str:
    return str(uuid.uuid4().int % 100_000_000)


def _random_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode("ascii")


def _random_key(length: int = 48) -> str:
    return base64.b64encode(secrets.token_bytes(length)).decode("ascii")


# 2.4.0 client's baked credential key (2.x deployments only, see IS_V2).
_CRED_AES_KEY = bytes.fromhex(
    os.getenv("CRED_AES_KEY_HEX", "00" * 32)
)
_CRED_AES_IV = bytes.fromhex(os.getenv("CRED_AES_IV_HEX", "00" * 16))


def _encrypt_credential(plaintext: str) -> str:
    """AES-256-CBC + PKCS7 + standard base64 with the client's baked key/IV.

    Mirrors exactly what the 2.4.0 client's credential decryptor expects, so
    the client's decrypt returns the same plaintext string we fed in.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = plaintext.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(_CRED_AES_KEY), modes.CBC(_CRED_AES_IV)).encryptor()
    return base64.b64encode(enc.update(raw) + enc.finalize()).decode("ascii")


def _append_login_log(request_body: dict, response_body: dict) -> None:
    """Append login request/response payloads (no headers) to a file."""
    LOGIN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = (
        f"[{timestamp}]\n"
        f"RequestBody: {json.dumps(request_body, ensure_ascii=False)}\n"
        f"ResponseBody: {json.dumps(response_body, ensure_ascii=False)}\n"
        f"{'=' * 80}\n"
    )
    with LOGIN_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(entry)


# Default account template for new players
DEFAULT_ACCOUNT = {
    "displayName": "",
    # Empty by default. The old "own everything" list was ~1600 2.4.0 item keys
    # that don't exist in 1.19.0 -> the client logs "pattern does not exist" for
    # each and grinds the buildable-room load. Owned items are set per-account via
    # the editor with VALID keys, not forced here.
    "ownedPatterns": [],
    "activeCosmetics": [],
    "ownedBundles": ALL_OWNED_BUNDLES,
    "wishlist": [],
    "eyeColor": -2039846657,
    "skinColor": -3489025,
    "mobileCode": "0000",
    "hasCreatorPack": 1,
    "hasUnlockedPrivateRooms": 1,
    "isIsolated": 0,
    "hasPendingMobileLogin": 0,
    # No roles by default: any role makes the client grind that role's permitted
    # build-item palette on buildable-room load. Grant roles per-account via the editor.
    "roleKeys": [],
    "currency": 999999999,
    "beans": 999999999,
    "redeemedStashIDs": [],
    "cw_admin": [],
    "cw_staff": [],
    "cw_favorites": [],
    "cw_nextCanUseFreeFuelTimestamp": "1970 01/01 00:00:00",
    "analyticEventKeys": [],
    "firstLogin": "",
    "loginStreakFreezeTimestamp": "1970 01/01 00:00:00",
    "loginStreakFreezeHistory": [],
    "hasPendingWarning": 0,
    "shouldForceRefresh": 0,
    "isPermabanned": 0,
    "isMutebanned": 0,
    "remainingBanHours": 0,
    "banReason": "",
    "adminBlocked": 0,   # our own block switch (editor Block button); 0 = can connect
    "skipAttestation": 0,
    "lastChallengeRedeemedTime_login": "1970 01/01 00:00:00",
    "lastChallengeRedeemedTime_easy": "1970 01/01 00:00:00",
    "lastChallengeRedeemedTime_hard": "1970 01/01 00:00:00",
    "pendingMessages": [],
    "loginStreakData": {"length": 1, "currentTier": "", "nextTierThreshold": 3, "nextTier": "bronze"},
    "matchmakingSegment": "default",
    # 1.31 HARD-REQUIRES this: the account parser (Gate A @ il2cpp 0x257570c)
    # does a TryGetValue("segmentationData") and aborts the whole parse
    # ("Failed to parse account data") if the key is absent. 1.19 never read it.
    # The 1.31 parser (Gate A @ il2cpp 0x257570c) reads these segmentationData
    # sub-keys, decoded from the binary:
    #   defaultSegmentKey : STRING, GetString + null-check -> parse fails if absent
    #   mapping/sessionThresholds/dayThresholds/base : DICTS (a JSON array casts to
    #     null -> NRE in the ctor's dict[key] lookup). So those four are objects.
    "segmentationData": {
        "defaultSegmentKey": "base",
        "mapping": {},
        "sessionThresholds": {},
        "dayThresholds": {},
        "base": {},
    },
    # New 1.31 top-level account fields the parser reads (guest/anonymous support);
    # 1.19 never had them. anonymous_displayName is GetString+null-checked.
    "anonymous": 0,
    "anonymous_displayName": "",
    # Fields the real client's login response includes that were missing
    # from our schema -- added so property lookups/parsing on these never
    # hit a missing key, the same class of bug as the loginStreakData fix.
    "achievementsInitDataVersion": 1,
    "anniversaryPuzzle_isComplete": 0,
    "areMobilePurchasesBlocked": 0,
    "backendVersion": 0,
    "banCategory": "",
    "banCount": 0,
    "banExpireTimestamp": "",
    "banWarnCounter": 0,
    "beanPurchasedCardCount": 0,
    "beans_total": 0,
    "contentCreatorCompletedChallenges": [],
    "creatorCodeLastRedeemedTime": "",
    "creatorCodesNumRedeemedThisWeek": 0,
    "crossover_GOG_accountID": "",
    "currency_cwPayout": 0,
    "cw_favoritesVersion": 1,
    "cw_freeFuelHistory": [],
    "cw_staffEditorSessionID": 0,
    "cw_vip": [],
    "cw_vipKeys": [],
    "dataVersion": 2,
    "displayName_searchable": "",
    "encryptedMobileCode": "",
    "encryptedMobileIDs": [],
    "encryptedMobilePassword": "",
    "encryptedMobileSessionID": "",
    "encryptedPublicSessionID": "",
    "encryptedQuestSessionID": "",
    "hasLinkedMobile": 0,
    "hashedChallengeNonce": "",
    "hoursSpent": 0,
    "lastCardBeanPurchaseTime": "",
    "lastCardChallengeRedeemedTime": "",
    "lastEditorSessionID": 0,
    "lastLogin": "",
    "lastLoginStatus": "",
    "lastValidatedEncryptedQuestSessionID": "",
    "loginStreakExpireTimestamp": "1970 01/01 00:00:00",
    "loginStreakLength": 0,
    "loginStreakLongestLastUpdatedTimestamp": "",
    "loginStreakLongestLength": 0,
    "loginStreakLongestPartition": "",
    "loginStreakPartition": "",
    "longestBanDuration": 0,
    "longestBanDuration_prev": 0,
    "lootQuotaProgress_lootQuotas_easy": {"count": 0, "lastUpdatedTime": ""},
    "lootQuotaProgress_lootQuotas_hard": {"count": 0, "lastUpdatedTime": ""},
    "lootQuotaProgress_lootQuotas_medium": {"count": 0, "lastUpdatedTime": ""},
    "mobileLastLogin": "",
    "mobileLinkFailCount": 0,
    "mobileLinkVersion": 0,
    "mobileLoginFailCount": 0,
    "mobileLoginFailTimestamp": "",
    "mobileNextCanLinkTimestamp": "",
    "mobileNextCanLoginTimestamp": "",
    "mobileSessionCount": 0,
    "nonceHistory_VR": [],
    "oculusIDFullHistory": [],
    "oculusIDHistory": [],
    "oculusID_searchable": "",
    "oculusNumber": "",
    "ownedPatterns_favorite": {},
    "ownedPatterns_hidden": [],
    "pendingBanDuration": 0,
    "pets": {},
    "pets_coinEvents": [],
    "promoCodeFailCount": 0,
    "purchases_started": 0,
    "purchases_successful": 0,
    "pv_coinsSpent_payout": 0,
    "pv_coinsSpent_total": 0,
    "pv_initialized": 1,
    "redeemedCardChallengeKeys": [],
    "redeemedPromoCodes": [],
    "reportCount": 0,
    "roleGivenTimestamps": {},
    "savedOutfits": [],
    "seenMessages": [],
    "sessionCount": 0,
    # Was "" - the client treats a blank spawnRoomKey as a fatal error
    # ("No spawn room key found") and never gets past the loading screen.
    # Needs to point at a room that actually exists in the seeded `rooms`
    # collection. All players spawn in official_1 (user directive).
    "spawnRoomKey": "official_1",
    "toymakerRequest": "",
    "warnCategories": [],
    "warnCount": 0,
    "warnReason": "",
}


# Name-based account bans. The raider's Meta accountID is unknown (assigned per
# headset), so we match on the oculusID / displayName instead: any name that
# normalizes (lowercase, non-alphanumerics stripped) to one of these is forced
# into the adminBlocked state below -> served permabanned + isolated, so the
# client refuses to join any room. Add new raider names here.
# ("banned_player" removed 2026-10-10 at the owner's request - they made up.)
BANNED_NAME_KEYS: set = set()

# The current, telemetry-free player build (single source of truth in config).
# NOTE: the game's login body does NOT carry the client version, so the real
# version enforcement is at the presence layer (see dynamo_ops: the shared
# G2_PlayerStatuses version is forced to this, so older clients fail the
# multiplayer join check). The checks below are a best-effort extra.
from config import REQUIRED_CLIENT_VERSION


# Discord link required to play (owner, 2026-10-11). An account with no Discord
# link is isolated with a message (the same screen as an outdated build) until it
# links with /link in our Discord. Owner and staff accounts are exempt.
REQUIRE_LINK = os.getenv("REQUIRE_DISCORD_LINK", "1") == "1"
_LINK_EXEMPT = set(filter(None, (os.getenv("LINK_EXEMPT_IDS") or
    "o_OWNER0000000001,o_PLAYER0000000003,o_STAFF00000000002").replace(" ", "").split(",")))
LINK_MESSAGE = ("Link your account first: in our Discord type /link username:<your Meta name> "
                "code:<your 4-digit mobile code>, then restart Yeeps.")


REQUIRE_GUILD = os.getenv("REQUIRE_DISCORD_GUILD", "1") == "1"
DISCORD_INVITE = os.getenv("DISCORD_INVITE_URL", "https://discord.gg/xJDR5c7qAj")
GUILD_MESSAGE = (f"You need to be in our Discord to play. Join at {DISCORD_INVITE}, then restart Yeeps.")


async def not_in_guild(account_id: str, account) -> bool:
    """A linked account whose Discord user has left the server (2026-10-11)."""
    if not REQUIRE_GUILD or not account_id:
        return False
    if account_id in _LINK_EXEMPT or account_id.startswith("o_99000000000000"):
        return False
    did = (account or {}).get("discordID")
    if not did:
        return False
    try:
        from services.discord_roles import is_guild_member
        return (await is_guild_member(str(did))) is False
    except Exception:
        return False


def link_required(account_id: str, account) -> bool:
    if not REQUIRE_LINK or not account_id:
        return False
    if account_id in _LINK_EXEMPT or account_id.startswith("o_99000000000000"):
        return False
    return not (account or {}).get("discordID")


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _is_banned_name(*names: str) -> bool:
    for n in names:
        k = _name_key(n)
        if k and any(b in k for b in BANNED_NAME_KEYS):
            return True
    return False


async def handle_login(body: dict) -> dict:
    """
    Process a login request. Auto-creates the account if it doesn't exist.
    Returns the 'data' payload (will be wrapped by the Lambda envelope).
    """
    account_id = body.get("accountID", "")
    oculus_id = body.get("oculusID", "")
    initial_skin = body.get("initialSkinColor", -3489025)
    initial_eye = body.get("initialEyeColor", -2039846657)
    props_to_get = body.get("propertiesToGet", [])

    if not account_id:
        # A blank/missing accountID means the request body didn't parse the
        # way we expect (e.g. arrived mid cold-start) -- creating an account
        # under "" produces an unusable, un-loggable-into account instead of
        # a clear error, so refuse instead of silently creating garbage.
        raise ValueError("Login request missing accountID")

    # Sanity-check the accountID so a public client can't flood the DB with
    # junk/injected accounts. Real IDs are short and alphanumeric-ish
    # (e.g. o_OWNER0000000001, test_account_123).
    if len(account_id) > 80 or not re.match(r"^[A-Za-z0-9_.\-]{1,80}$", account_id):
        raise ValueError("Invalid accountID")

    # Effective default = DEFAULT_ACCOUNT merged with the editor's override
    # (globals key "default_account_override"), so the editor site can change
    # what every new player starts with (owned items, roles, currency, ...)
    # without a redeploy.
    effective_default = dict(DEFAULT_ACCOUNT)
    try:
        from services.db import globals_col
        _ov = await globals_col.find_one({"key": "default_account_override"},
                                         {"_id": 0, "value": 1})
        if _ov and isinstance(_ov.get("value"), dict):
            effective_default.update(_ov["value"])
    except Exception as _e:
        print(f"[login] default override load failed: {_e}")

    # Look up or create account
    account = await accounts.find_one({"accountID": account_id}, {"_id": 0})
    _is_new_account = account is None

    if account is None:
        # Auto-create new account
        account = dict(effective_default)
        account["accountID"] = account_id
        from services.names import unique_name
        account["displayName"] = await unique_name(oculus_id or account_id, account_id)
        account["skinColor"] = initial_skin
        account["eyeColor"] = initial_eye
        account["firstLogin"] = _server_time()
        await accounts.insert_one({**account})
        print(f"[login] Created new account: {account_id} ({oculus_id})")
    else:
        print(f"[login] Existing account: {account_id}")
        # Backfill any default keys added since this account was created, so
        # older accounts pick up new fields without a reset.
        for key, default_val in effective_default.items():
            if key not in account:
                account[key] = default_val

    # Names must show something in game (no blank / invisible-character / hidden
    # rich-text names, owner's rule 2026-10-10): swap one for the Meta name.
    from services.names import is_visible_name, fallback_name
    if not is_visible_name(account.get("displayName")):
        _new_name = fallback_name(account.get("oculusID") or oculus_id, account_id)
        from services.names import unique_name
        _new_name = await unique_name(_new_name, account_id)
        print(f"[login] {account_id}: invisible name {account.get('displayName')!r} -> {_new_name!r}")
        account["displayName"] = _new_name
        await accounts.update_one({"accountID": account_id}, {"$set": {
            "displayName": _new_name, "displayName_searchable": _new_name.lower()}})

    # Headset check (services/device_auth.py): binds this headset on a trusted
    # login, flags (or, when enforcing, refuses) a login from another headset.
    # Runs before this login's IP is stamped, so a stranger can't "earn" the IP.
    from services import device_auth
    _device_ok, _device_note = await device_auth.check_login(
        account, account_id, str(body.get("_deviceKey") or ""),
        str(body.get("_clientIP") or "").strip(), _is_new_account)

    # Banned headset (services/device_bans.py): an account that this headset
    # really owns (its key is bound to the account, e.g. a brand-new account it
    # just created) is banned too, so a new Meta account doesn't get a banned
    # player back in. A banned key sent for SOMEONE ELSE's account must not ban
    # that account (Oct 10: the raider sent fake logins with his banned key to
    # get other players banned) - that login is just refused.
    _refuse_banned_dk = False
    try:
        from services.device_bans import is_device_banned
        _raw_dk = str(body.get("_deviceKey") or "")
        _owns = False
        if await is_device_banned(_raw_dk):
            from services import device_auth as _da2
            _da2.forget(account_id)
            _bk = await _da2._keys_of(account_id) or []
            _owns = _da2.key_hash(_raw_dk) in _bk
            if not _owns:
                print(f"[device] banned headset tried to log in as {account_id} (not its account) - refused "
                      f"ip={body.get('_clientIP', '')}")
                _refuse_banned_dk = True
        # Only AUTO-ban on a banned-headset match when device-auth is in
        # ENFORCE mode. In observe mode (the default - see services/device_auth.py)
        # a device-key match must never silently permaban a real account; we
        # just log it. This stops false auto-bans of legit players.
        if _owns and not account.get("adminBlocked") and not _da2.enforcing():
            print(f"[device] {account_id} owns a banned headset but device-auth is OBSERVE "
                  f"- NOT auto-banning (would have, enforce mode) ip={body.get(_clientIP,)}")
        elif _owns and not account.get("adminBlocked"):
            _why = "Banned headset"
            account.update(adminBlocked=1, isPermabanned=1, banReason=_why, blockReason=_why, banExpireTimestamp="")
            await accounts.update_one({"accountID": account_id}, {"$set": {
                "adminBlocked": 1, "isPermabanned": 1, "banReason": _why, "blockReason": _why,
                "banCategory": "blocked", "banExpireTimestamp": ""}})
            print(f"[device] {account_id} logged in from a BANNED headset -> account banned too "
                  f"ip={body.get('_clientIP', '')}")
            try:
                from routers.editor_router import _discord_notify
                await _discord_notify(f"A banned headset logged into **{account.get('displayName') or account_id}** "
                                      f"(`{account_id}`) - that account is now banned too.")
            except Exception:
                pass
    except Exception as _e:
        print(f"[device] banned-headset check failed for {account_id}: {_e}")
    if _refuse_banned_dk:
        # A clean refusal (not an exception, which the server answered with a 500).
        return {"data": {"success": 0, "error": "NOT_AUTHORIZED"}}

    # Guard against stale/legacy accounts with a null currentTier, which
    # crashes the client's string parsing for login streak data.
    if account.get("loginStreakData", {}).get("currentTier") is None:
        account["loginStreakData"] = {**account.get("loginStreakData", {}), "currentTier": ""}
    if account.get("matchmakingSegment") is None:
        account["matchmakingSegment"] = "default"
    if account.get("loginStreakFreezeHistory") is None:
        account["loginStreakFreezeHistory"] = []
    if not account.get("spawnRoomKey"):
        account["spawnRoomKey"] = "official_1"

    # Buttcoins/beans are unlimited on this server: refill anyone who ran low
    # (accounts copied from real Yeeps started at 0 and couldn't buy anything).
    _refill = {}
    for _cur in ("currency", "beans"):
        try:
            if int(account.get(_cur) or 0) < 100_000_000:
                _refill[_cur] = 999999999
        except Exception:
            _refill[_cur] = 999999999
    if _refill:
        account.update(_refill)
        await accounts.update_one({"accountID": account_id}, {"$set": _refill})

    # FORCE-normalize segmentationData every login (do NOT rely on backfill):
    # accounts that logged in before this field existed already have it stored,
    # and may have it in a stale/wrong shape -- the 1.31 parser's SegmentationData
    # ctor reads mapping/sessionThresholds/dayThresholds/base and NREs ("Failed to
    # parse account data") unless all four are dictionaries. Overwrite unconditionally,
    # same pattern as the baked e_accessKey/e_secretKey normalization.
    _sd = account.get("segmentationData")
    if (not isinstance(_sd, dict)
            or not isinstance(_sd.get("defaultSegmentKey"), str)
            or not all(isinstance(_sd.get(k), dict)
                       for k in ("mapping", "sessionThresholds", "dayThresholds", "base"))):
        account["segmentationData"] = {
            "defaultSegmentKey": "base",
            "mapping": {}, "sessionThresholds": {}, "dayThresholds": {}, "base": {},
        }
    # New 1.31 guest/anonymous fields: force-present (existing accounts predate them).
    if not isinstance(account.get("anonymous"), int):
        account["anonymous"] = 0
    if not isinstance(account.get("anonymous_displayName"), str):
        account["anonymous_displayName"] = account.get("displayName") or ""

    # Banned-name block: a raider (e.g. "giggle__shitter") can't just make a new
    # account under that name -- any matching oculusID/displayName is forced into
    # adminBlocked (served permabanned + isolated below, can't join any room).
    if _is_banned_name(oculus_id, account.get("displayName")):
        account["adminBlocked"] = 1
        account["blockReason"] = "Blocked by server admin"
        try:
            await accounts.update_one(
                {"accountID": account_id},
                {"$set": {"adminBlocked": 1, "blockReason": account["blockReason"]}},
            )
        except Exception as _e:
            print(f"[login] banned-name persist failed for {account_id}: {_e}")
        print(f"[login] BANNED NAME blocked: {account_id} ({oculus_id})")

    # BAN CARRYOVER (2.31.4 -> 1.31): a staff Block on the biglive server
    # (Yeeps240) also bans here, so a raider banned on either apk is out on both.
    # Checked live every login (NOT persisted), so lifting the biglive ban also
    # lifts it here. Only adminBlocked (persistent staff Block) carries, honouring
    # a timed ban's expiry.
    if not account.get("adminBlocked"):
        try:
            import os as _os
            _obd = _os.getenv("OTHER_BAN_DB", "Yeeps240")
            if _obd:
                from services.db import client as _mc
                _b240 = await _mc[_obd]["accounts"].find_one(
                    {"accountID": account_id},
                    {"_id": 0, "adminBlocked": 1, "banExpireTimestamp": 1,
                     "banReason": 1, "blockReason": 1})
                if _b240 and _b240.get("adminBlocked"):
                    _still = True
                    _bexp = _b240.get("banExpireTimestamp")
                    if _bexp:
                        try:
                            _still = datetime.strptime(_bexp, "%Y %m/%d %H:%M:%S").replace(
                                tzinfo=timezone.utc) > datetime.now(timezone.utc)
                        except Exception:
                            _still = True
                    if _still:
                        account["adminBlocked"] = 1
                        account["blockReason"] = (_b240.get("blockReason") or _b240.get("banReason")
                                                  or "Banned on Yeeps 2.31.4")
                        if _bexp:
                            account["banExpireTimestamp"] = _bexp
                            account["banReason"] = _b240.get("banReason") or account["blockReason"]
                        print(f"[ban] carryover from 2.31.4: {account_id} blocked "
                              f"(reason={account['blockReason']!r})")
        except Exception as _e:
            print(f"[ban] 2.31.4 carryover check failed for {account_id}: {_e}")

    # No per-login overrides of items/roles/currency: serve whatever the account
    # holds in the DB (set via the editor). New accounts get the DEFAULT_ACCOUNT
    # values. (Owned patterns and roles are empty by default on purpose — see the
    # notes on DEFAULT_ACCOUNT — because invalid/oversized sets grind the 1.19.0
    # buildable-room load.)

    # Persist the merged/backfilled state -- previously only new-account
    # creation wrote to the DB, so existing accounts' backfilled fields and
    # ownership grants only ever lived in the response, never in Mongo
    # (invisible to the editor UI, and re-computed from scratch every login
    # instead of actually sticking).
    await accounts.update_one(
        {"accountID": account_id},
        {"$set": {k: v for k, v in account.items() if k != "accountID"}},
    )

    # Stamp the caller's IP on the account (lastIP + last 5 recentIPs) so an admin
    # can ban a raider by username from the editor without hunting the logs.
    _ip = str(body.get("_clientIP") or "").strip()
    # Never remember the IP of a login the headset check refused: otherwise a
    # second try from the same IP would count as "a network this account plays
    # from" and could claim the account.
    if _ip and _ip != "?" and _device_ok:
        _prior_ips = list(account.get("recentIPs") or [])
        try:
            await accounts.update_one(
                {"accountID": account_id},
                {"$set": {"lastIP": _ip},
                 "$push": {"recentIPs": {"$each": [_ip], "$slice": -5}}})
        except Exception as _e:
            print(f"[login] IP stamp failed for {account_id}: {_e}")
        # ALERT (not ban) when this account logs in from an IP we've never seen
        # for it. Skip first-ever login (no prior history). Pushing _ip above
        # means we only alert once per new IP (it's "known" on the next join).
        if _prior_ips and _ip not in _prior_ips:
            try:
                import asyncio
                _t = asyncio.create_task(_alert_new_ip(
                    account_id, account.get("displayName"), account.get("oculusID"),
                    _ip, _prior_ips))
                _bg_tasks.add(_t)
                _t.add_done_callback(_bg_tasks.discard)
            except Exception as _e:
                print(f"[login] new-IP alert schedule failed for {account_id}: {_e}")

    # Build response from the full account payload to match legacy behavior.
    # Older clients often tolerate/expect additional fields beyond
    # propertiesToGet, so we include all known account fields.
    # Never private fields: login only takes an accountID (no proof), so
    # whatever it returns is readable by anyone who knows that id.
    from services.account_privacy import PRIVATE_ACCOUNT_FIELDS
    data = {k: v for k, v in account.items() if k != "accountID" and k not in PRIVATE_ACCOUNT_FIELDS}
    if oculus_id:
        data["oculusID"] = oculus_id          # the caller's own, as sent

    # Role-locked blocks (services/role_locks.py): e.g. the BLAZE block is only
    # owned/placeable with the server-only "blaze" role.
    from services import role_locks
    await role_locks.load_blaze_holders()
    if role_locks.BLAZE_ROLE in (account.get("roleKeys") or []) and not role_locks.is_blaze_holder(account_id):
        account["roleKeys"] = [r for r in account.get("roleKeys") or [] if r != role_locks.BLAZE_ROLE]
        await accounts.update_one({"accountID": account_id}, {"$pull": {"roleKeys": role_locks.BLAZE_ROLE}})
        print(f"[roles] removed blaze from {account_id} (not on the owner's blaze list)")
    data["ownedPatterns"], data["roleKeys"] = role_locks.apply(
        data.get("ownedPatterns"), account.get("roleKeys"), account_id)

    # Ensure requested properties always exist.
    for prop in props_to_get:
        if prop not in data:
            data[prop] = default_for_property(prop)

    # Always refresh serverTime (per-request, safe).
    data["serverTime"] = _server_time()
    data["skipAttestation"] = 1
    # Leave gameSessionID / publicSessionID / challengeNonce / mobileID /
    # mobilePassword as the stored account carries them (regenerated below only
    # if missing). NOTE: e_accessKey / e_secretKey are (re)written further down
    # from the served creds using the client's FIXED baked AES key — the client
    # does NOT derive its decrypt key from the session fields (that earlier
    # theory was disproven by live key capture), so the encrypted blobs do not
    # need to "match" a particular login's session IDs.

    # ── Version gate: require the current "blaze" build ─────────────────
    # The current player build reports applicationVersion/trueVersion
    # "1.19.0-blaze" (manifest versionName). OLDER builds (which still ship
    # player telemetry to GameAnalytics) report "1.19.0". We block anything
    # that is not the current build so stale, telemetry-leaking clients can't
    # play until they reinstall. Override the required string with env
    # REQUIRED_CLIENT_VERSION if the build tag changes later.
    # The login body usually has NO version (the game doesn't send one here). We
    # ONLY block when the login body itself proves an outdated version — we do
    # NOT block on the learned `lastClientVersion`, because right after a player
    # updates, that stored value is still the OLD build and would wrongly lock
    # them out (and they'd never get un-stuck). lastClientVersion is recorded for
    # visibility only (see dynamo_ops). Reliable per-client blocking needs the
    # client to send its version at login — the updated APK can be made to inject
    # it, after which this gate becomes airtight.
    _client_ver = str(body.get("trueVersion") or body.get("applicationVersion") or "").strip()
    _last_ver = str(account.get("lastClientVersion") or "").strip()
    # `trueVersion` is the game's internal version (for example "1.31.0"),
    # while the host's REQUIRED_CLIENT_VERSION may include the mod suffix
    # ("1.31.0-blaze"). It is not a reliable APK identity and must not be used
    # to block players. The signed native build tag is checked below instead.
    _outdated_client = False

    # Build identity/integrity gate (services/build_gate.py): the 1.31+ APK sends
    # its build tag in the login path. Record it on the account (so staff can see
    # who runs which APK, and Photon auth / refresh can enforce it this session),
    # and block a missing/old build here with the visible "update" message.
    # "modified" builds are NOT blocked here: Photon auth denies them covertly.
    try:
        from services.build_gate import classify as _bclassify, enforced as _benforced
        _btag = str(body.get("_clientBuildTag") or "")
        _bstatus = _bclassify(_btag)
        # Only the account's own headset updates its recorded build: a refused
        # login from somewhere else (2026-10-10, a script posing as the owner)
        # used to overwrite it and mark the real player's build outdated.
        if _device_ok:
            await accounts.update_one({"accountID": account_id}, {"$set": {
                "clientBuildTag": _btag, "clientBuildStatus": _bstatus,
                "clientBuildSeenAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")}})
        account["clientBuildStatus"] = _bstatus
        if _bstatus != "ok":
            print(f"[build] {account_id} tag='{_btag or '-'}' -> {_bstatus}")
        if _benforced() and _bstatus == "outdated":
            _outdated_client = True
        # Testing toggle: "require newest build" (editor Online tab). While set,
        # every client not on that build is outdated -> kicked + kept out, so
        # staff can clear the server mid-test. The newest-build tester stays in.
        from services.build_gate import require_build as _req_build, require_blocks as _req_blocks
        _reqb = await _req_build()
        if _benforced() and _req_blocks(_btag, _reqb):
            _outdated_client = True
    except Exception as _e:
        print(f"[build] gate failed for {account_id}: {_e}")
    if _client_ver or _last_ver:
        print(f"[login] version {account_id}: body='{_client_ver or '-'}' "
              f"lastSeen='{_last_ver or '-'}' (need '{REQUIRED_CLIENT_VERSION}') "
              f"blocked={_outdated_client}")

    # Force every account to the required build string so all (allowed) players
    # match for the pw / multiplayer map-sync gate, which compares the master
    # player's stored version against the joiner's reported version. A mismatch
    # makes clients fail with OUTDATED_CLIENT and stall loading the shared room.
    data["applicationVersion"] = REQUIRED_CLIENT_VERSION
    # dataVersion mirrors what the client sent (2 for 2.x); keep in sync.
    if "dataVersion" in body:
        data["dataVersion"] = body.get("dataVersion")
    # backendVersion 0 is the 1.x placeholder; 2.x expects a live backend.
    if not data.get("backendVersion"):
        data["backendVersion"] = 1

    # Ban / isolation state.
    #
    # By DEFAULT nobody is banned: the client's RoomMapManager.
    # GetHasPermissionToJoinRoom refuses to join ANY room (incl. public hubs)
    # for a banned/isolated account -> it connects to Master then stalls
    # forever. The real accounts can be permabanned ("Modding"), so we clear
    # that every login -> normal players always connect, and in-game bans never
    # stick (they're overwritten here on the next login).
    #
    # ADMIN BLOCK: setting `adminBlocked` = 1 on an account (via the editor's
    # Block button) flips this -> we serve them as permabanned + isolated, so
    # the client refuses to join any room and they can't get into multiplayer.
    # This is OUR block switch, independent of the game's own ban system.
    #
    # Auto-expire a TIMED staff ban: g2_moderatorBanAccount sets adminBlocked + a
    # banExpireTimestamp; once it passes, clear the block so the ban lifts itself.
    if account.get("adminBlocked") and account.get("banExpireTimestamp"):
        try:
            _exp = datetime.strptime(account["banExpireTimestamp"], "%Y %m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if _exp <= datetime.now(timezone.utc):
                account["adminBlocked"] = 0
                await accounts.update_one({"accountID": account_id},
                    {"$set": {"adminBlocked": 0, "banExpireTimestamp": "", "banReason": ""}})
        except Exception:
            pass
    _unlinked = link_required(account_id, account)
    _not_member = (not _unlinked) and await not_in_guild(account_id, account)
    try:
        from services.build_gate import closed_message as _closed_message
        _closed = await _closed_message()
    except Exception:
        _closed = ""
    if _closed:
        # The game is closed: same isolation screen, with the closed message.
        _cls_exp = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc)
        data["isPermabanned"] = 1
        data["isIsolated"] = 1
        data["isMutebanned"] = 1
        data["banCategory"] = "closed"
        data["banReason"] = _closed
        data["banExpireTimestamp"] = _cls_exp.strftime("%Y %m/%d %H:%M:%S")
        data["remainingBanHours"] = 1
        data["hasPendingWarning"] = 0
    elif _outdated_client:
        # Outdated build → isolate it exactly like an admin block (client
        # refuses to join any room), with a message telling them to update.
        # This is NOT persisted as adminBlocked, so the moment they reinstall
        # the current build they're back to normal with no leftover ban.
        # Served as a short TIMED suspension (isPermabanned + a real expiry +
        # matching remainingBanHours), NOT the no-expiry permaban screen: the
        # player isn't banned, they just need to update. The window is
        # re-issued on every login, so it never lapses into a playable state.
        _upd_exp = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 3600,
                                          tz=timezone.utc)
        data["isPermabanned"] = 1
        data["isIsolated"] = 1
        data["isMutebanned"] = 1
        data["banCategory"] = "outdated"
        data["banReason"] = outdated_message()
        data["banExpireTimestamp"] = _upd_exp.strftime("%Y %m/%d %H:%M:%S")
        data["remainingBanHours"] = 1
        data["hasPendingWarning"] = 0
    elif _unlinked or _not_member:
        # Not linked to Discord yet, or they've left the server: same isolation screen.
        _lnk_exp = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc)
        data["isPermabanned"] = 1
        data["isIsolated"] = 1
        data["isMutebanned"] = 1
        data["banCategory"] = "unlinked"
        data["banReason"] = LINK_MESSAGE if _unlinked else GUILD_MESSAGE
        data["banExpireTimestamp"] = _lnk_exp.strftime("%Y %m/%d %H:%M:%S")
        data["remainingBanHours"] = 1
        data["hasPendingWarning"] = 0
    elif not _device_ok:
        # Not this account's headset (enforce mode only). Timed like the
        # outdated block: nothing persisted, re-issued every login.
        _dev_exp = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 3600,
                                          tz=timezone.utc)
        data["isPermabanned"] = 1
        data["isIsolated"] = 1
        data["isMutebanned"] = 1
        data["banCategory"] = "device"
        data["banReason"] = (
            "This account is locked to another headset. If it's yours (new or reset headset), "
            "ask an admin to reset your headset lock." if _device_note == "other-headset" else
            "This headset couldn't be verified for this account (new internet connection?). "
            "Ask an admin to reset your headset lock, then log in again.")
        data["banExpireTimestamp"] = _dev_exp.strftime("%Y %m/%d %H:%M:%S")
        data["remainingBanHours"] = 1
        data["hasPendingWarning"] = 0
    elif account.get("adminBlocked"):
        data["isPermabanned"] = 1
        data["isIsolated"] = 1
        data["isMutebanned"] = 1
        data["banCategory"] = "blocked"
        data["banReason"] = "you are banned -blaze"
        _exp_ts = account.get("banExpireTimestamp") or ""
        data["banExpireTimestamp"] = _exp_ts
        # Serve the REAL remaining hours for a timed ban so the client shows the
        # right duration. A permanent ban (no expiry) stays 99999. Previously we
        # always served 99999, which made a short timed ban look permanent.
        if _exp_ts:
            try:
                _exp = datetime.strptime(_exp_ts, "%Y %m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
                _rem_h = (_exp - datetime.now(timezone.utc)).total_seconds() / 3600.0
                data["remainingBanHours"] = max(1, -(-int(_rem_h * 3600) // 3600))  # ceil hours, min 1
            except Exception:
                data["remainingBanHours"] = 99999
        else:
            data["remainingBanHours"] = 99999
        data["hasPendingWarning"] = 0
    else:
        data["isPermabanned"] = 0
        data["isMutebanned"] = 0
        data["isIsolated"] = 0
        data["banCount"] = 0
        data["banCategory"] = "unspecified"
        data["banExpireTimestamp"] = ""
        data["banReason"] = ""
        data["remainingBanHours"] = 0
        data["pendingBanDuration"] = 0
        # A staff WARN is a one-shot KICK. g2_moderatorWarnAccount set
        # hasPendingWarning + shouldForceRefresh; the sticky force-refresh makes
        # the client re-pull here at its next room transition. On THAT login we
        # isolate them exactly like a ban (client refuses to stay/join -> booted
        # out of the game) with a warn message, then clear the flag immediately
        # so their NEXT login is clean and they rejoin normally. Net effect: a
        # warn kicks the player once; a ban (adminBlocked) keeps them out.
        if account.get("pendingKick"):
            # One-shot KICK (services/kick.py): isolate this single re-pull with a
            # short timed suspension showing the reason, then clear the flag so
            # the player can relaunch and rejoin right away. Not a ban.
            _kr = account.get("kickReason") or DEFAULT_KICK_REASON
            _kick_exp = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 60,
                                               tz=timezone.utc)
            data["isPermabanned"] = 1
            data["isIsolated"] = 1
            data["isMutebanned"] = 1
            data["banCategory"] = "kicked"
            data["banReason"] = _kr
            data["banExpireTimestamp"] = _kick_exp.strftime("%Y %m/%d %H:%M:%S")
            data["remainingBanHours"] = 1
            data["hasPendingWarning"] = 0
            await accounts.update_one({"accountID": account_id},
                                      {"$set": {"pendingKick": 0, "kickReason": ""}})
        elif account.get("hasPendingWarning"):
            _wr = account.get("warnReason") or "You have been warned -blaze"
            # Serve a SHORT banExpireTimestamp so the client shows a brief TIMED
            # kick ("suspended until ...") instead of "You are permanently
            # banned." (isPermabanned=1 with NO expiry = the permaban screen).
            _now = datetime.now(timezone.utc)
            _warn_exp = datetime.fromtimestamp(_now.timestamp() + 60, tz=timezone.utc)
            data["hasPendingWarning"] = 1
            data["warnReason"] = _wr
            data["warnCount"] = account.get("warnCount", 1)
            data["isPermabanned"] = 1
            data["isIsolated"] = 1
            data["isMutebanned"] = 1
            data["banCategory"] = "warned"
            data["banReason"] = _wr
            data["banExpireTimestamp"] = _warn_exp.strftime("%Y %m/%d %H:%M:%S")
            data["remainingBanHours"] = 1
            await accounts.update_one({"accountID": account_id}, {"$set": {"hasPendingWarning": 0}})
        else:
            data["hasPendingWarning"] = 0

    # This login IS a full account refresh, so it consumes any pending
    # shouldForceRefresh (ban/warn sets it; dynamo_ops serves it sticky until
    # the client re-pulls here). Clear it + its in-memory stickiness timer so it
    # fires exactly once and can't loop.
    if account.get("shouldForceRefresh"):
        try:
            from services.dynamo_ops import _clear_force_refresh
            await _clear_force_refresh(account_id)
        except Exception:
            await accounts.update_one({"accountID": account_id}, {"$set": {"shouldForceRefresh": 0}})

    # 1.31: a brand-new account has no e_accessKey/e_secretKey until /link
    # copies in the player's real ones, and without them the client shows its
    # fatal-error screen right after login (it calls fetchGlobalFatalError and
    # never loads). Every unlinked new player hit this on 2026-10-10. These are
    # the shared guest creds (see services/account_privacy.py), so give such
    # accounts the owner's working pair; a later /link overwrites them.
    from config import IS_V2 as _IS_V2_CREDS, DEFAULT_MAP_OWNER as _CRED_DONOR
    if not _IS_V2_CREDS and not (data.get("e_accessKey") and data.get("e_secretKey")):
        try:
            _donor = await accounts.find_one(
                {"accountID": _CRED_DONOR, "e_accessKey": {"$nin": [None, ""]},
                 "e_secretKey": {"$nin": [None, ""]}},
                {"_id": 0, "e_accessKey": 1, "e_secretKey": 1})
            if _donor:
                data["e_accessKey"] = _donor["e_accessKey"]
                data["e_secretKey"] = _donor["e_secretKey"]
                await accounts.update_one({"accountID": account_id}, {"$set": _donor})
                print(f"[login] {account_id}: gave the shared e_* guest creds (none stored)")
        except Exception as _e:
            print(f"[login] e_* creds fill failed for {account_id}: {_e}")

    # Credentials — version-dependent shape:
    #   * The 2.x client decrypts the encrypted e_accessKey/e_secretKey blobs
    #     with a client-baked key.
    #   * The 1.19.0 client reads PLAINTEXT accessKey/secretKey/sessionToken
    #     directly from the login payload (confirmed via the 1.19.0 metadata
    #     dump: the account-field constants class defines "accessKey",
    #     "secretKey", "sessionToken" — and NOT e_accessKey — and the client
    #     does no AES decryption during "Processing account data"). If those
    #     plaintext fields are absent it logs "Account: Failed to get
    #     credentials" -> "Failed to fetch online account data" and never goes
    #     online. So we must include the plaintext trio.
    # The client signs its direct DynamoDB calls with these, but the proxy
    # ignores AWS SigV4, so the creds only need to be present and well-formed.
    # Use real Cognito guest creds when reachable (valid ~1h, genuine format),
    # else well-formed placeholders. We serve BOTH the plaintext trio (1.19.0)
    # and the stored encrypted blobs (2.x) so one payload works for either.
    creds = None
    try:
        creds = await get_guest_credentials()
    except Exception as _e:
        print(f"[login] guest cred fetch failed: {_e}")
    if creds:
        data["accessKey"] = creds["accessKey"]
        data["secretKey"] = creds["secretKey"]
        data["sessionToken"] = creds["sessionToken"]
    else:
        # Well-formed, non-null placeholders (proxy ignores the signature).
        data["accessKey"] = "ASIAYEEPSVRPROXY0000"
        data["secretKey"] = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY12"
        data["sessionToken"] = "FQoGZXIvYXdzEProxyPlaceholderSessionToken0000000000000000"

    # 2.x client: it decrypts e_accessKey/e_secretKey with its baked AES key, so
    # re-encrypt the served creds with that key (2.x deployment AND 2.x client).
    from config import IS_V2 as _IS_V2
    if _IS_V2:
        _cv = str(body.get("trueVersion") or body.get("applicationVersion") or "")
        try:
            _is_2x = int(_cv.split(".")[0]) >= 2
        except (ValueError, IndexError):
            _is_2x = str(body.get("dataVersion")) == "2"
        if _is_2x:
            try:
                data["e_accessKey"] = _encrypt_credential(data["accessKey"])
                data["e_secretKey"] = _encrypt_credential(data["secretKey"])
            except Exception as _e:
                print(f"[login] credential encryption failed: {_e}")

    # Session identifiers required by the 1.19.0 credential check. During
    # "Processing account data" the AccountManager coroutine reads
    # e_accessKey/e_secretKey plus the session IDs and calls
    # String.IsNullOrEmpty on them — if publicSessionID or challengeNonce is
    # empty it bails with "Account: Failed to get credentials" (verified live
    # via a field-read trace). The real backend mints these per login; the
    # captured account JSON never carried them, so generate non-empty values
    # for any that are missing. gameSessionID is already present on the
    # account. Persist so the editor and 2.x path see a consistent set.
    _sess_added = {}
    # Persist the required build version so other players (and the editor) read
    # a consistent version for this account. Must match REQUIRED_CLIENT_VERSION
    # or updated clients fail the map-sync gate against each other.
    if data.get("applicationVersion") != REQUIRED_CLIENT_VERSION:
        data["applicationVersion"] = REQUIRED_CLIENT_VERSION
    _sess_added["applicationVersion"] = REQUIRED_CLIENT_VERSION
    if not data.get("gameSessionID"):
        data["gameSessionID"] = _random_session_id()
        _sess_added["gameSessionID"] = data["gameSessionID"]
    if not data.get("publicSessionID"):
        data["publicSessionID"] = _random_session_id()
        _sess_added["publicSessionID"] = data["publicSessionID"]
    if not data.get("challengeNonce"):
        data["challengeNonce"] = _random_nonce()
        _sess_added["challengeNonce"] = data["challengeNonce"]
    if _sess_added:
        await accounts.update_one({"accountID": account_id}, {"$set": _sess_added})

    result = {"data": data}
    # Expose credentials + session identifiers at the RESPONSE ROOT as well.
    # The 1.19.0 client reads the plaintext accessKey/secretKey/sessionToken
    # (and the session/mobile fields) from the top level of the login payload,
    # not from inside "data" — with no AES decryption and no hash check during
    # "Processing account data" (confirmed live: zero crypto activity when it
    # logs "Failed to get credentials"). Mirroring them to the root lets the
    # 1.19.0 reader find them while the nested "data" copy keeps 2.x working.
    for _k in ("accessKey", "secretKey", "sessionToken",
               "e_accessKey", "e_secretKey",
               "mobileID", "mobilePassword", "mobileCode",
               "gameSessionID", "publicSessionID", "challengeNonce",
               "hash", "banHash", "hashProperties"):
        if _k in data:
            result[_k] = data[_k]
    _append_login_log(body, result)
    return result
