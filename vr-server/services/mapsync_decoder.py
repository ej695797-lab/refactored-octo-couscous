"""
Decode a Yeeps 1.19 map blob (base64 .NET BinaryFormatter / MS-NRBF) back into a
plain list of blocks, the inverse of services.mapsync_serializer.serialize_blocks.

Two shapes are produced by the game / our tooling:

  * MapSyncData           - mapObjects: MapObjectSyncData[]   (our serializer's output)
      each element has: key, placedPosition(Vector3Int), placedForwardFacing,
      placedUpFacing, colorKey, ownerUserID
  * MapSyncDataCompressed - the client's own in-game save: index tables
      keys[] / colorKeys[] / ownerUserIDs[] plus a packed mapObjectByteStream of
      12-byte records:  [typeIdx u16][x u16][y u16][z u16][rot u8][colorIdx u8][ownerIdx u16]
      rot: forwardFacing = high nibble, upFacing = low nibble.

decode_map(base64_str) -> list[ {name,x,y,z,fwd,up,color,owner} ]  (or [] if empty/unknown)
"""

import base64
import struct


class _R:
    """Minimal MS-NRBF reader: collects every object by id, enough to pull out
    the byte array and string arrays we need. Not a general deserializer."""

    def __init__(self, buf: bytes):
        self.b = buf
        self.i = 0
        self.objects: dict[int, object] = {}
        self.order: list[int] = []   # object ids in stream order

    # primitive readers
    def u8(self):
        v = self.b[self.i]; self.i += 1; return v

    def i32(self):
        v = struct.unpack_from("<i", self.b, self.i)[0]; self.i += 4; return v

    def s(self):
        # length-prefixed (7-bit encoded) UTF-8 string
        n = 0; shift = 0
        while True:
            x = self.b[self.i]; self.i += 1
            n |= (x & 0x7F) << shift
            if not (x & 0x80):
                break
            shift += 7
        v = self.b[self.i:self.i + n].decode("utf-8", "replace"); self.i += n
        return v

    def _skip_type_info(self, bt):
        # additional type-info per BinaryTypeEnum
        if bt == 0:            # Primitive
            self.u8()
        elif bt == 3:          # SystemClass
            self.s()
        elif bt == 4:          # Class
            self.s(); self.i32()
        elif bt == 7:          # PrimitiveArray
            self.u8()
        # 1 String, 2 Object, 5 ObjectArray, 6 StringArray -> no extra

    def _read_member_value(self, bt, prim):
        # Only primitive members carry an inline value; everything else is a
        # following record (ref/null/nested) we parse in the main loop.
        if bt == 0:
            self._read_primitive(prim)
            return False
        return True  # value is the next record

    def _read_primitive(self, pt):
        sz = {1: 1, 2: 1, 3: 2, 4: 2, 5: 1, 6: 8, 7: 8, 8: 4, 9: 8,
              10: 4, 11: 1, 13: 8, 14: 4, 15: 8, 16: 8, 18: None}.get(pt)
        if pt == 18:   # String
            self.s()
        elif sz:
            self.i += sz

    def parse(self):
        b = self.b
        while self.i < len(b):
            tag = self.u8()
            if tag == 0:            # SerializedStreamHeader
                self.i += 16
            elif tag == 12:         # BinaryLibrary
                self.i32(); self.s()
            elif tag == 11:         # MessageEnd
                break
            elif tag == 6:          # BinaryObjectString
                oid = self.i32(); val = self.s()
                self.objects[oid] = val; self.order.append(oid)
            elif tag == 10:         # ObjectNull
                pass
            elif tag == 13:         # ObjectNullMultiple256
                self.u8()
            elif tag == 14:         # ObjectNullMultiple
                self.i32()
            elif tag == 9:          # MemberReference
                self.i32()
            elif tag == 15:         # ArraySinglePrimitive
                oid = self.i32(); length = self.i32(); pt = self.u8()
                if pt == 2:         # Byte
                    data = b[self.i:self.i + length]; self.i += length
                    self.objects[oid] = ("bytes", data); self.order.append(oid)
                else:
                    for _ in range(length):
                        self._read_primitive(pt)
            elif tag == 17:         # ArraySingleString
                oid = self.i32(); length = self.i32()
                vals = []
                for _ in range(length):
                    t = self.u8()
                    if t == 6:
                        soid = self.i32(); sv = self.s()
                        self.objects[soid] = sv; vals.append(sv)
                    elif t == 13:   # null run (256)
                        cnt = self.u8(); vals.extend([""] * cnt)
                    elif t == 14:
                        cnt = self.i32(); vals.extend([""] * cnt)
                    elif t == 10:
                        vals.append("")
                    elif t == 9:
                        self.i32(); vals.append("")
                    else:
                        break
                self.objects[oid] = ("strs", vals); self.order.append(oid)
            elif tag == 16:         # ArraySingleObject
                oid = self.i32(); length = self.i32()
                self.objects[oid] = ("objarr", length); self.order.append(oid)
            elif tag == 5:          # ClassWithMembersAndTypes
                self._class_with_members()
            elif tag == 1:          # ClassWithId (reuses metadata) -> stop detailed parse
                # We don't need these for the compressed layout; bail to the
                # simpler array-collection path.
                raise _NeedArrays()
            else:
                raise _NeedArrays()
        return self

    def _class_with_members(self):
        self.i32()                       # objectId
        self.s()                         # class name
        mc = self.i32()
        [self.s() for _ in range(mc)]    # member names
        bts = [self.u8() for _ in range(mc)]
        prims = []
        for bt in bts:
            if bt == 0:
                prims.append(self.u8())
            else:
                prims.append(None)
                self._skip_type_info_after(bt)
        self.i32()                       # libraryId
        # member values: primitive inline, else a following record
        for bt, pr in zip(bts, prims):
            if bt == 0:
                self._read_primitive(pr)
            # non-primitive members are separate records parsed by the main loop

    def _skip_type_info_after(self, bt):
        if bt == 3:
            self.s()
        elif bt == 4:
            self.s(); self.i32()
        elif bt == 7:
            self.u8()


class _NeedArrays(Exception):
    pass


def _decode_compressed(reader: "_R") -> list:
    # In member order the class references mapObjectByteStream, keys, colorKeys,
    # ownerUserIDs. In the stream, the byte array and the three string arrays
    # appear in that same order.
    byte_arr = None
    str_arrays = []
    for oid in reader.order:
        obj = reader.objects.get(oid)
        if isinstance(obj, tuple) and obj[0] == "bytes" and byte_arr is None:
            byte_arr = obj[1]
        elif isinstance(obj, tuple) and obj[0] == "strs":
            str_arrays.append(obj[1])
    if byte_arr is None:
        return []
    keys = str_arrays[0] if len(str_arrays) > 0 else []
    colors = str_arrays[1] if len(str_arrays) > 1 else []
    owners = str_arrays[2] if len(str_arrays) > 2 else []
    out = []
    n = len(byte_arr) // 12
    for r in range(n):
        o = r * 12
        ti, x, y, z = struct.unpack_from("<HHHH", byte_arr, o)
        rot = byte_arr[o + 8]
        cidx = byte_arr[o + 9]
        oidx = struct.unpack_from("<H", byte_arr, o + 10)[0]
        out.append({
            "name": keys[ti] if ti < len(keys) else "",
            "x": x, "y": y, "z": z,
            "fwd": (rot >> 4) & 0x0F, "up": rot & 0x0F,
            "color": colors[cidx] if cidx < len(colors) else "",
            "owner": owners[oidx] if oidx < len(owners) else "",
        })
    return out


def _decode_spam(raw: bytes) -> list:
    """Decode the 1.31 SPAM blob: "SPAM" u8 ver, then keys[]/colorKeys[]/
    ownerUserIDs[] (.NET BinaryWriter strings) + a 12-byte-record byte stream."""
    pos = 5  # skip "SPAM" magic + 1-byte version

    def rd_i32():
        nonlocal pos
        v = struct.unpack_from("<i", raw, pos)[0]; pos += 4; return v

    def rd_str():
        nonlocal pos
        n = shift = 0
        while True:
            byte = raw[pos]; pos += 1
            n |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
        s = raw[pos:pos + n].decode("utf-8", "replace"); pos += n
        return s

    def rd_table():
        return [rd_str() for _ in range(rd_i32())]

    keys = rd_table(); colors = rd_table(); owners = rd_table()
    blen = rd_i32()
    stream = raw[pos:pos + blen]
    out = []
    for i in range(0, (len(stream) // 12) * 12, 12):
        ti, x, y, z, rot, ci, oi = struct.unpack_from("<HHHHBBH", stream, i)
        out.append({
            "name": keys[ti] if ti < len(keys) else "",
            "x": x, "y": y, "z": z,
            "fwd": (rot >> 4) & 0xF, "up": rot & 0xF,
            "color": colors[ci] if ci < len(colors) else "",
            "owner": owners[oi] if oi < len(owners) else "",
        })
    return out


def decode_map(save_b64: str) -> list:
    if not save_b64 or not isinstance(save_b64, str):
        return []
    try:
        raw = base64.b64decode(save_b64)
    except Exception:
        return []
    if raw[:4] == b"SPAM":          # 1.31 flat binary
        try:
            return _decode_spam(raw)
        except Exception:
            return []
    kind = "compressed" if b"MapSyncDataCompressed" in raw[:400] else "plain"
    reader = _R(raw)
    try:
        reader.parse()
    except _NeedArrays:
        pass
    except Exception:
        pass
    if kind == "compressed":
        return _decode_compressed(reader)
    # plain MapSyncData: elements are MapObjectSyncData objects. The string
    # members (key, colorKey, ownerUserID) land in objects as plain strings in
    # element order [key, colorKey, ownerUserID] per block; positions/facings are
    # inline primitives we skipped. For editing we expose at least the keys.
    strs = [reader.objects[o] for o in reader.order if isinstance(reader.objects[o], str)]
    # Best-effort: group every 3 strings into one block (key,color,owner).
    out = []
    for i in range(0, len(strs) - 2, 3):
        out.append({"name": strs[i], "color": strs[i + 1], "owner": strs[i + 2]})
    return out
