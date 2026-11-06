"""
Serialize a list of map blocks into the .NET BinaryFormatter (MS-NRBF) stream
the Yeeps 1.19.0 client deserializes into a `MapSyncData { MapObjectSyncData[]
mapObjects }`.  This is the SAME format as the known-good empty-map blob
(services.dynamo_ops.EMPTY_MAP_SAVE_DATA) — just with a non-empty array.

Block shape (as produced by the /map API):
    {name, x, y, z, fwd, up, color, owner}  (sx/sy/sz are ignored — size comes
    from the block's own definition in-game).

MapObjectSyncData struct (from the 1.19.0 metadata dump):
    string      key                  -> block name
    Vector3Int  placedPosition       -> (x, y, z)
    int         placedForwardFacing  -> fwd
    int         placedUpFacing       -> up
    string      colorKey             -> color
    string      ownerUserID          -> owner

NRBF reference: [MS-NRBF]. Record type enums used here:
  0 SerializedStreamHeader, 1 ClassWithId, 5 ClassWithMembersAndTypes,
  6 BinaryObjectString, 7 BinaryArray, 10 ObjectNull, 11 MessageEnd,
  12 BinaryLibrary.
BinaryType enums: 0 Primitive, 1 String, 4 Class.  PrimitiveType 8 = Int32.
"""

import base64
import struct

ASM_CSHARP = "Assembly-CSharp, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null"
ASM_UNITY  = "UnityEngine.CoreModule, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null"


def _i32(v: int) -> bytes:
    return struct.pack("<i", int(v))


def _len_prefixed(s: str) -> bytes:
    """NRBF length-prefixed UTF-8 string: 7-bit-encoded length then bytes."""
    data = s.encode("utf-8")
    n = len(data)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out) + data


class _Writer:
    def __init__(self):
        self.buf = bytearray()
        self._next_id = 1

    def new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def w(self, b: bytes):
        self.buf.extend(b)

    # -- records --------------------------------------------------------
    def header(self, root_id: int):
        self.w(bytes([0]))
        self.w(_i32(root_id)); self.w(_i32(-1)); self.w(_i32(1)); self.w(_i32(0))

    def binary_library(self, lib_id: int, name: str):
        self.w(bytes([12])); self.w(_i32(lib_id)); self.w(_len_prefixed(name))

    def object_string(self, obj_id: int, s: str):
        self.w(bytes([6])); self.w(_i32(obj_id)); self.w(_len_prefixed(s))

    def message_end(self):
        self.w(bytes([11]))


def serialize_blocks(blocks) -> str:
    """Return base64 NRBF for MapSyncData with the given blocks. blocks is a list
    of dicts with name/x/y/z/fwd/up/color/owner (missing -> sensible defaults)."""
    if not blocks:
        # Reuse the known-good empty-map blob verbatim for 0 blocks.
        from services.dynamo_ops import EMPTY_MAP_SAVE_DATA
        return EMPTY_MAP_SAVE_DATA

    wr = _Writer()
    root_id = wr.new_id()        # 1 : MapSyncData
    lib_cs = 2
    lib_unity = 3
    wr._next_id = 4              # object ids start after reserved lib ids

    wr.header(root_id)
    wr.binary_library(lib_cs, ASM_CSHARP)
    wr.binary_library(lib_unity, ASM_UNITY)

    # --- MapSyncData (ClassWithMembersAndTypes) ---
    arr_id = wr._next_id; wr._next_id += 1   # BinaryArray object id
    wr.w(bytes([5]))                         # ClassWithMembersAndTypes
    wr.w(_i32(root_id))
    wr.w(_len_prefixed("MapSyncData"))
    wr.w(_i32(1))                            # member count
    wr.w(_len_prefixed("mapObjects"))
    wr.w(bytes([4]))                         # MemberType: Class
    wr.w(_len_prefixed("MapObjectSyncData[]")); wr.w(_i32(lib_cs))  # ClassTypeInfo
    wr.w(_i32(lib_cs))                       # library id of MapSyncData
    # member value: reference to the array
    wr.w(bytes([9])); wr.w(_i32(arr_id))     # MemberReference -> array

    # --- BinaryArray of MapObjectSyncData ---
    wr.w(bytes([7]))                         # BinaryArray
    wr.w(_i32(arr_id))
    wr.w(bytes([0]))                         # BinaryArrayType: Single
    wr.w(_i32(1))                            # rank
    wr.w(_i32(len(blocks)))                  # length
    wr.w(bytes([4]))                         # element BinaryType: Class
    wr.w(_len_prefixed("MapObjectSyncData")); wr.w(_i32(lib_cs))    # ClassTypeInfo

    mos_meta_id = None   # object id whose MapObjectSyncData metadata we reuse
    v3_meta_id = None    # object id whose Vector3Int metadata we reuse

    for b in blocks:
        name = str(b.get("name", "") or "")
        x = int(b.get("x", 0) or 0); y = int(b.get("y", 0) or 0); z = int(b.get("z", 0) or 0)
        fwd = int(b.get("fwd", 0) or 0); up = int(b.get("up", 0) or 0)
        color = str(b.get("color", "") or "")
        owner = str(b.get("owner", "") or "")

        obj_id = wr._next_id; wr._next_id += 1
        if mos_meta_id is None:
            mos_meta_id = obj_id
            # ClassWithMembersAndTypes for MapObjectSyncData (first element)
            wr.w(bytes([5])); wr.w(_i32(obj_id))
            wr.w(_len_prefixed("MapObjectSyncData"))
            wr.w(_i32(6))
            for m in ("key", "placedPosition", "placedForwardFacing",
                      "placedUpFacing", "colorKey", "ownerUserID"):
                wr.w(_len_prefixed(m))
            # MemberTypeInfo: BinaryTypes then AdditionalInfos
            wr.w(bytes([1, 4, 0, 0, 1, 1]))          # String,Class,Primitive,Primitive,String,String
            wr.w(_len_prefixed("UnityEngine.Vector3Int")); wr.w(_i32(lib_unity))  # Class info (placedPosition)
            wr.w(bytes([8]))                         # Primitive: Int32 (placedForwardFacing)
            wr.w(bytes([8]))                         # Primitive: Int32 (placedUpFacing)
            wr.w(_i32(lib_cs))                       # library id of MapObjectSyncData
        else:
            # ClassWithId reusing the first element's metadata
            wr.w(bytes([1])); wr.w(_i32(obj_id)); wr.w(_i32(mos_meta_id))

        # --- member values, in declaration order ---
        # key (String)
        sid = wr._next_id; wr._next_id += 1
        wr.object_string(sid, name)
        # placedPosition (Vector3Int)
        v3_id = wr._next_id; wr._next_id += 1
        if v3_meta_id is None:
            v3_meta_id = v3_id
            wr.w(bytes([5])); wr.w(_i32(v3_id))
            wr.w(_len_prefixed("UnityEngine.Vector3Int"))
            wr.w(_i32(3))
            for m in ("m_X", "m_Y", "m_Z"):
                wr.w(_len_prefixed(m))
            wr.w(bytes([0, 0, 0]))                   # three Int32 primitives
            wr.w(bytes([8, 8, 8]))
            wr.w(_i32(lib_unity))                    # library id of Vector3Int
        else:
            wr.w(bytes([1])); wr.w(_i32(v3_id)); wr.w(_i32(v3_meta_id))
        wr.w(_i32(x)); wr.w(_i32(y)); wr.w(_i32(z))
        # placedForwardFacing / placedUpFacing (Int32 inline)
        wr.w(_i32(fwd)); wr.w(_i32(up))
        # colorKey (String)
        sid = wr._next_id; wr._next_id += 1
        wr.object_string(sid, color)
        # ownerUserID (String)
        sid = wr._next_id; wr._next_id += 1
        wr.object_string(sid, owner)

    wr.message_end()
    return base64.b64encode(bytes(wr.buf)).decode("ascii")


def extract_blocks(pasted) -> list:
    """Accept the raw text/obj the user pastes from the /map API and return the
    block list. Accepts the full {ok, roomKey, blocks:[...]} envelope, a bare
    list of blocks, or a JSON string of either."""
    import json
    obj = pasted
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("utf-8", "replace")
    if isinstance(obj, str):
        obj = json.loads(obj)
    if isinstance(obj, dict):
        blocks = obj.get("blocks")
        if blocks is None and "mapObjects" in obj:
            blocks = obj["mapObjects"]
        obj = blocks
    if not isinstance(obj, list):
        raise ValueError("No 'blocks' array found in pasted map data")
    return obj


def serialize_blocks_compressed(blocks) -> str:
    """Return base64 NRBF for a MapSyncDataCompressed with the given blocks.

    This is the compact format the 1.19 client itself saves and reads reliably
    for large maps (the uncompressed MapSyncData from serialize_blocks() stalls
    the client past a few hundred objects). Layout reversed from a real save:
    class MapSyncDataCompressed { byte[] mapObjectByteStream; string[] keys,
    colorKeys, ownerUserIDs } where each 12-byte record is
    [typeIdx u16][x u16][y u16][z u16][rot u8 = fwd<<4|up][colorIdx u8][ownerIdx u16].
    """
    import struct
    if not blocks:
        from services.dynamo_ops import EMPTY_MAP_SAVE_DATA
        return EMPTY_MAP_SAVE_DATA

    keys, kidx = [], {}
    colors, cidx = [], {}
    owners, oidx = [], {}

    def _idx(lst, mp, v):
        v = v or ""
        if v not in mp:
            mp[v] = len(lst); lst.append(v)
        return mp[v]

    records = bytearray()
    for b in blocks:
        ti = _idx(keys, kidx, str(b.get("name", "") or ""))
        x = int(b.get("x", 0) or 0) & 0xFFFF
        y = int(b.get("y", 0) or 0) & 0xFFFF
        z = int(b.get("z", 0) or 0) & 0xFFFF
        rot = ((int(b.get("fwd", 0) or 0) & 0xF) << 4) | (int(b.get("up", 0) or 0) & 0xF)
        ci = _idx(colors, cidx, str(b.get("color", "") or "")) & 0xFF
        oi = _idx(owners, oidx, str(b.get("owner", "") or "")) & 0xFFFF
        records += struct.pack("<HHHHBBH", ti, x, y, z, rot, ci, oi)

    buf = bytearray()
    buf += bytes([0]) + _i32(1) + _i32(-1) + _i32(1) + _i32(0)        # SerializationHeader
    buf += bytes([12]) + _i32(2) + _len_prefixed(ASM_CSHARP)          # BinaryLibrary id=2
    buf += bytes([5]) + _i32(1) + _len_prefixed("MapSyncDataCompressed")  # ClassWithMembersAndTypes oid=1
    buf += _i32(4)
    for m in ("mapObjectByteStream", "keys", "colorKeys", "ownerUserIDs"):
        buf += _len_prefixed(m)
    buf += bytes([7, 6, 6, 6])      # BinaryTypeEnums: PrimitiveArray, String[], String[], String[]
    buf += bytes([2])               # PrimitiveArray additional info: Byte
    buf += _i32(2)                  # library id
    for ref in (3, 4, 5, 6):        # member values: MemberReference to each array oid
        buf += bytes([9]) + _i32(ref)
    # oid 3: byte[] mapObjectByteStream
    buf += bytes([15]) + _i32(3) + _i32(len(records)) + bytes([2]) + bytes(records)
    # oid 4/5/6: string[] arrays; their string elements get fresh ids from 7 up
    next_oid = 7

    def _str_arr(oid, strs):
        nonlocal next_oid
        out = bytearray()
        out += bytes([17]) + _i32(oid) + _i32(len(strs))
        for s in strs:
            out += bytes([6]) + _i32(next_oid) + _len_prefixed(s)
            next_oid += 1
        return bytes(out)

    buf += _str_arr(4, keys)
    buf += _str_arr(5, colors)
    buf += _str_arr(6, owners)
    buf += bytes([11])              # MessageEnd
    return base64.b64encode(bytes(buf)).decode("ascii")


def _bw_string(s: str) -> bytes:
    """.NET BinaryWriter string: 7-bit-encoded length prefix, then UTF-8 bytes."""
    data = (s or "").encode("utf-8")
    n = len(data)
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out) + data


def serialize_blocks_spam(blocks) -> str:
    """Return base64 SPAM for the given blocks — the flat binary format the
    1.31 client saves/reads (mapDataVersion 3). Layout (little-endian):
      "SPAM" | u8 version=1 | i32 nKeys + keys[] | i32 nColors + colorKeys[] |
      i32 nOwners + ownerUserIDs[] | i32 byteLen + mapObjectByteStream
    strings are .NET BinaryWriter strings; each 12-byte record is identical to
    the 1.19 compressed stream: [keyIdx u16][x u16][y u16][z u16]
    [rot u8 = fwd<<4|up][colorIdx u8][ownerIdx u16]. An empty map is 21 bytes.
    """
    import struct
    keys, kidx = [], {}
    colors, cidx = [], {}
    owners, oidx = [], {}

    def _idx(lst, mp, v):
        v = v or ""
        if v not in mp:
            mp[v] = len(lst); lst.append(v)
        return mp[v]

    records = bytearray()
    for b in (blocks or []):
        ti = _idx(keys, kidx, str(b.get("name", "") or ""))
        x = int(b.get("x", 0) or 0) & 0xFFFF
        y = int(b.get("y", 0) or 0) & 0xFFFF
        z = int(b.get("z", 0) or 0) & 0xFFFF
        rot = ((int(b.get("fwd", 0) or 0) & 0xF) << 4) | (int(b.get("up", 0) or 0) & 0xF)
        ci = _idx(colors, cidx, str(b.get("color", "") or "")) & 0xFF
        oi = _idx(owners, oidx, str(b.get("owner", "") or "")) & 0xFFFF
        records += struct.pack("<HHHHBBH", ti, x, y, z, rot, ci, oi)

    def _table(lst):
        out = bytearray(struct.pack("<i", len(lst)))
        for s in lst:
            out += _bw_string(s)
        return bytes(out)

    buf = bytearray(b"SPAM")
    buf += bytes([1])                       # version (saveData)
    buf += _table(keys)
    buf += _table(colors)
    buf += _table(owners)
    buf += struct.pack("<i", len(records)) + bytes(records)
    return base64.b64encode(bytes(buf)).decode("ascii")
