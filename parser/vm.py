"""DVD virtual-machine command decoder.

Decodes the 8-byte DVD VM commands found in a PGC command table
(pre-commands, post-commands, cell commands) into the ODVD spec
section 8.2 JSON form.

Encoding reference (all multi-byte values big-endian):
  * dvd.sourceforge.net/dvdinfo/vmi.html  (instruction set broken down
    by value; libdvdnav's reference table)
  * en.wikibooks.org/wiki/Inside_DVD-Video/Instruction_Set_Details
  * freemkv.org/docs/dvd/  ("Command encoding" section)

Layout summary:
  byte 0: bits 7-5 = command family (type), bit 4 = immediate/direct
          flag, bits 3-0 = sub-command / set operation.
  byte 1: bit 7 = compare-operand-is-immediate flag, bits 6-4 =
          compare operator (0 = unconditional), bits 3-0 = sub-command
          (link sub-command for Set families).
  bytes 2-7: operands, per family (see decode_command()).

Compare operators: 0 none, 1 AND-nonzero, 2 equal, 3 not-equal,
4 greater-or-equal, 5 greater, 6 less-or-equal, 7 less.

A command that carries a compare guard is emitted as
{"op": "compare", "operator": ..., "left": {...}, "right": ...,
 "then": <inner command>, "else": null}.  The "else" is fall-through
to the next command in the list, which is exactly what the disc
does, so it is always null; it is present so players can see the
shape from the spec example.

Anything that cannot be decoded is returned with status
"unsupported" and the object {"op": "unsupported", "raw_hex": ...,
"note": ...} -- it is never silently dropped (spec section 8.2).
Compound set/compare/link commands (families 4-6) are decoded
best-effort with status "partial": their operand packing is not
pinned down by any public source we could verify, so the raw bytes
and the fields we are sure about are emitted together with
"decode": "partial".  See README.md.
"""

from __future__ import annotations

PARSER_VERSION = "0.1.0"

CMP_NAMES = {
    0: None,
    1: "and_nonzero",
    2: "eq",
    3: "ne",
    4: "ge",
    5: "gt",
    6: "le",
    7: "lt",
}

SET_OPS = {
    0: "nop",
    1: "mov",
    2: "swap",
    3: "add",
    4: "sub",
    5: "mul",
    6: "div",
    7: "mod",
    8: "random",
    9: "and",
    10: "or",
    11: "xor",
}

# Link-subset target codes (byte 7 low 5 bits, "Link" family sub 1
# and the trailing link of the Set families).
LINK_CODES = {
    0: "none",
    1: "top_cell",
    2: "next_cell",
    3: "prev_cell",
    5: "top_pg",
    6: "next_pg",
    7: "prev_pg",
    9: "top_pgc",
    10: "next_pgc",
    11: "prev_pgc",
    12: "goup_pgc",
    13: "tail_pgc",
    16: "resume",
}

MENU_NAMES = {
    0: "none",
    2: "title",
    3: "root",
    4: "subpicture",
    5: "audio",
    6: "angle",
    7: "chapter",
}


def _u16(b: bytes, off: int) -> int:
    return (b[off] << 8) | b[off + 1]


def reg_spec(value: int) -> dict:
    """Decode a register operand byte.

    Values below 16 name a GPRM; values >= 0x80 name SPRM
    (value - 0x80).  Anything in between is not a valid register
    reference on a real disc; it is reported raw so a player can
    see exactly what the disc said.
    """
    if value < 16:
        return {"gprm": value}
    if value >= 0x80:
        return {"sprm": value - 0x80}
    return {"reg_raw": value}


def _reg_or_raw(value: int) -> dict:
    return reg_spec(value)


def _compare_guard(b: bytes, left_byte: int, right_kind: str):
    """Build the compare-guard dict for a guarded command.

    Returns None when the command is unconditional (compare op 0).

    right_kind: "imm16" (right operand is a 16-bit immediate in the
    two bytes after the left byte), "reg16" (right operand is a
    16-bit immediate in bytes 6-7, else a register in byte 7), or
    "byte" (right operand is byte 7: immediate value if the flag
    is set, else a register).
    """
    cmp_op = (b[1] >> 4) & 0x07
    if cmp_op == 0:
        return None
    imm = bool(b[1] & 0x80)
    left = _reg_or_raw(b[left_byte])
    if right_kind == "imm16":
        if imm:
            right = {"value": _u16(b, left_byte + 1)}
        else:
            right = _reg_or_raw(b[left_byte + 2])
    elif right_kind == "reg16":
        if imm:
            right = {"value": _u16(b, 6)}
        else:
            right = _reg_or_raw(b[7])
    elif right_kind == "byte":
        if imm:
            right = {"value": b[7]}
        else:
            right = _reg_or_raw(b[7])
    else:  # pragma: no cover - internal misuse
        raise ValueError(right_kind)
    guard = {
        "op": "compare",
        "operator": CMP_NAMES[cmp_op],
        "left": left,
        "right": right,
        "then": None,  # filled in by the caller
        "else": None,
    }
    # Spec section 8.2 example shape: {"op": "compare", "gprm": 3,
    # "value": 2, "then": {...}, "else": {...}} -- include the flat
    # aliases when the comparison is exactly GPRM vs immediate.
    if "gprm" in left and "value" in right:
        guard["gprm"] = left["gprm"]
        guard["value"] = right["value"]
    return guard


def _wrap(guard, inner):
    if guard is None:
        return inner
    guard["then"] = inner
    return guard


def _unsupported(b: bytes, note: str) -> tuple[dict, str]:
    return (
        {"op": "unsupported", "raw_hex": b.hex(), "note": note},
        "unsupported",
    )


def _link_subset(b: bytes):
    """Decode a Link-subset target (link sub-command 1).

    hl_bn is byte 6 bits 7-2 (button to highlight, 0 = unchanged);
    the link code is byte 7 bits 4-0.
    """
    hl_bn = b[6] >> 2
    code = b[7] & 0x1F
    name = LINK_CODES.get(code)
    obj = {"op": "link"}
    if hl_bn:
        obj["highlight_button"] = hl_bn
    if code == 0:
        obj["target"] = "none"
    elif name is None:
        return None  # unknown link code -> caller marks unsupported
    else:
        obj["target"] = name
    return obj


def _trailing_link(b: bytes):
    """Decode the trailing link carried by Set families.

    Byte 1 bits 3-0 select the link sub-command; bytes 6-7 hold
    its operands.  Returns None when there is no trailing link.
    """
    sub = b[1] & 0x0F
    if sub == 0:
        return None
    if sub == 1:
        return _link_subset(b)
    if sub == 4:
        return {"op": "link_pgc", "pgc": _u16(b, 6) & 0x7FFF}
    if sub == 5:
        obj = {"op": "link_ptt", "chapter": _u16(b, 6) & 0x03FF}
        if b[6] >> 2:
            obj["highlight_button"] = b[6] >> 2
        return obj
    if sub == 6:
        obj = {"op": "link_program", "program": b[7] & 0x7F}
        if b[6] >> 2:
            obj["highlight_button"] = b[6] >> 2
        return obj
    if sub == 7:
        obj = {"op": "link_cell", "cell": b[7]}
        if b[6] >> 2:
            obj["highlight_button"] = b[6] >> 2
        return obj
    return "invalid"


def _jump_ss(b: bytes, op_name: str) -> dict:
    """Decode a JumpSS / CallSS target selector (byte 5 bits 7-6)."""
    sel = b[5] >> 6
    menu_id = b[5] & 0x0F
    if sel == 0:
        obj = {"op": op_name, "target": "first_play"}
    elif sel == 1:
        obj = {"op": op_name, "target": "vmgm_menu",
               "menu": menu_id, "menu_name": MENU_NAMES.get(menu_id, "unknown")}
    elif sel == 2:
        if op_name == "call_ss":
            # CallSS carries the resume cell in byte 4, so the
            # title/VTS move one byte earlier than in JumpSS:
            # byte 2 = title, byte 3 = VTS, byte 4 = resume cell.
            obj = {"op": op_name, "target": "vts_menu", "vts": b[3],
                   "title": b[2], "menu": menu_id,
                   "menu_name": MENU_NAMES.get(menu_id, "unknown")}
        else:
            obj = {"op": op_name, "target": "vts_menu", "vts": b[4],
                   "title": b[3], "menu": menu_id,
                   "menu_name": MENU_NAMES.get(menu_id, "unknown")}
    else:  # sel == 3
        obj = {"op": op_name, "target": "vmgm_pgc",
               "pgc": _u16(b, 2) & 0x7FFF}
    if op_name == "call_ss" and b[4] and sel != 3:
        obj["resume_cell"] = b[4]
    elif op_name == "call_ss" and sel == 3 and b[4]:
        obj["resume_cell"] = b[4]
    return obj


def _decode_set_stn(b: bytes, imm: bool) -> dict:
    """SetSTN: set audio / subpicture / angle (SPRM 1/2/3)."""
    if imm:
        audio = (b[3] & 0x7F) if b[3] & 0x80 else None
        sub = (b[4] & 0x7F) if b[4] & 0x80 else None
        angle = (b[5] & 0x7F) if b[5] & 0x80 else None
        vals = {"audio": audio, "subtitle": sub, "angle": angle}
    else:
        # Register source: the 7-bit field names a register.  In
        # practice disc authors use a GPRM here; anything else is
        # reported raw rather than guessed.
        def _src(byte):
            if byte & 0x80:
                v = byte & 0x7F
                return {"gprm": v} if v < 16 else {"reg_raw": v}
            return None

        vals = {"audio": _src(b[3]), "subtitle": _src(b[4]),
                "angle": _src(b[5])}
    set_keys = [k for k, v in vals.items() if v is not None]
    if len(set_keys) == 1:
        k = set_keys[0]
        op = {"audio": "set_audio", "subtitle": "set_subtitle",
              "angle": "set_angle"}[k]
        field = {"audio": "stream", "subtitle": "stream",
                 "angle": "angle"}[k]
        return {"op": op, field: vals[k]}
    obj = {"op": "set_stn"}
    for k in ("audio", "subtitle", "angle"):
        if vals[k] is not None:
            obj[k] = vals[k]
    if not set_keys:
        obj["note"] = "SetSTN with no stream flag set"
    return obj


def _decode_type2(b: bytes):
    """SetSystem family (byte 0 bits 7-5 == 2)."""
    sub = b[0] & 0x0F
    imm = bool(b[0] & 0x10)
    link_sub = b[1] & 0x0F
    cmp_op = (b[1] >> 4) & 0x07
    if cmp_op and link_sub:
        return _unsupported(
            b, "SetSystem with both a compare guard and a trailing "
               "link is illegal (CMP & LNK conflict)")
    if sub == 0:
        inner = {"op": "nop"}
    elif sub == 1:
        inner = _decode_set_stn(b, imm)
    elif sub == 2:  # SetNVTMR
        if imm:
            value = {"value": _u16(b, 2)}
        else:
            value = {"source": _reg_or_raw(b[3])}
        inner = {"op": "set_nvtmr", "pgc": b[5], **value}
    elif sub == 3:  # SetGPRMMD
        if imm:
            value = {"value": _u16(b, 2)}
        else:
            value = {"source": _reg_or_raw(b[3])}
        inner = {"op": "set_gprmmd", "gprm": b[5] & 0x0F,
                 "counter_mode": bool(b[5] & 0x80), **value}
    elif sub == 4:  # SetAMXMD (karaoke mixing mode, SPRM 11)
        if imm:
            value = {"value": _u16(b, 4)}
        else:
            value = {"source": _reg_or_raw(b[5])}
        inner = {"op": "set_amxmd", **value}
    elif sub == 6:  # SetHL_BTNN (highlighted button, SPRM 8)
        if imm:
            v = _u16(b, 4)
            inner = {"op": "set_hl_btnn", "value": v}
            if v % 1024 == 0 and v:
                inner["button"] = v // 1024
        else:
            inner = {"op": "set_hl_btnn", "source": _reg_or_raw(b[5])}
    else:
        return _unsupported(b, f"unknown SetSystem sub-command {sub}")
    if link_sub:
        link = _trailing_link(b)
        if link == "invalid":
            return _unsupported(b, f"invalid trailing link sub-command {link_sub}")
        if link is not None:
            inner["link"] = link
    guard = _compare_guard(b, 6, "byte")
    return _wrap(guard, inner), ("ok" if guard is None or True else "ok")


def _decode_type3(b: bytes):
    """SetGPRM family (byte 0 bits 7-5 == 3)."""
    imm = bool(b[0] & 0x10)
    op_code = b[0] & 0x0F
    link_sub = b[1] & 0x0F
    cmp_op = (b[1] >> 4) & 0x07
    if cmp_op and link_sub:
        return _unsupported(
            b, "SetGPRM with both a compare guard and a trailing "
               "link is illegal (CMP & LNK conflict)")
    if op_code == 0:
        inner = {"op": "nop"}
    elif op_code not in SET_OPS:
        return _unsupported(b, f"unknown SetGPRM operation {op_code}")
    else:
        op_name = SET_OPS[op_code]
        dest = b[3] & 0x0F
        if imm:
            src = {"value": _u16(b, 4)}
        else:
            # Register source goes under "source": spreading it
            # flat would collide with the destination "gprm" key
            # when the source is itself a GPRM (real-disc bug found
            # 2026-11-22: GPRM7 = GPRM6 decoded as GPRM6 = GPRM6).
            src = {"source": _reg_or_raw(b[5])}
        inner = {"op": "set", "gprm": dest, "operation": op_name}
        if op_name == "swap":
            # Swap exchanges the destination with the register named
            # by the low 4 bits of byte 5.
            inner["with_gprm"] = b[5] & 0x0F
        else:
            inner.update(src)
    if link_sub:
        link = _trailing_link(b)
        if link == "invalid":
            return _unsupported(b, f"invalid trailing link sub-command {link_sub}")
        if link is not None:
            inner["link"] = link
    guard = _compare_guard(b, 2, "reg16")
    return _wrap(guard, inner), "ok"


def _decode_compound(b: bytes, family: int):
    """Families 4-6: Set/Compare/Link compounds.

    Best-effort only (see module docstring): the set operation,
    compare operator, destination GPRM, highlight button and link
    target are decoded; the set source and compare operand bytes
    are carried raw because their packing is not pinned down by
    any verifiable public source.
    """
    set_op = SET_OPS.get(b[0] & 0x0F, "unknown")
    cmp_op = CMP_NAMES.get((b[1] >> 4) & 0x07)
    link = _link_subset(b)
    if link is None:
        return _unsupported(b, "compound command with unknown link code")
    names = {4: "set_compare_link", 5: "compare_set_link",
             6: "compare_set_link_always"}
    obj = {
        "op": names[family],
        "gprm": b[2] & 0x0F,
        "set_operation": set_op,
        "set_source_immediate": bool(b[0] & 0x10),
        "set_source_raw_hex": b[3:5].hex(),
        "compare_operator": cmp_op,
        "compare_operand_immediate": bool(b[1] & 0x80),
        "compare_operand_raw_hex": b[4:6].hex(),
        "link": link,
        "raw_hex": b.hex(),
        "decode": "partial",
        "note": ("compound Set/Compare/Link operand packing is "
                 "provisional in v0.1; see parser README.md"),
    }
    if b[6] >> 2:
        obj["highlight_button"] = b[6] >> 2
    return obj, "partial"


def decode_command(raw: bytes):
    """Decode one 8-byte VM command.

    Returns (obj, status) where status is "ok", "partial" or
    "unsupported".  obj is always a JSON-able dict and always
    carries enough information to identify the raw command.
    """
    if len(raw) != 8:
        return _unsupported(bytes(raw), f"command is {len(raw)} bytes, expected 8")
    b = bytes(raw)
    family = b[0] >> 5
    if family == 7:
        return _unsupported(
            b, "command family 7 has never been observed on a real "
               "disc (dvd.sourceforge.net); no defined meaning")
    if family in (4, 5, 6):
        return _decode_compound(b, family)

    if family == 0:  # Special
        sub = b[1] & 0x0F
        if sub == 0:
            inner = {"op": "nop"}
        elif sub == 1:
            inner = {"op": "goto", "line": b[7]}
        elif sub == 2:
            inner = {"op": "break"}
        elif sub == 3:
            inner = {"op": "set_tmp_pml", "level": b[6] & 0x0F,
                     "line": b[7]}
        else:
            return _unsupported(b, f"unknown Special sub-command {sub}")
        guard = _compare_guard(b, 3, "imm16")
        return _wrap(guard, inner), "ok"

    if family == 1:  # Link (bit 4 = 0) or Jump/Call (bit 4 = 1)
        sub = b[1] & 0x0F
        if b[0] & 0x10:  # Jump / Call
            if sub == 0:
                inner = {"op": "nop"}
            elif sub == 1:
                inner = {"op": "exit"}
            elif sub == 2:
                inner = {"op": "jump_tt", "title": b[5] & 0x7F}
            elif sub == 3:
                inner = {"op": "jump_vts_tt", "title": b[5] & 0x7F}
            elif sub == 5:
                inner = {"op": "jump_vts_ptt", "title": b[5] & 0x7F,
                         "chapter": _u16(b, 2) & 0x03FF}
            elif sub == 6:
                inner = _jump_ss(b, "jump_ss")
            elif sub == 8:
                inner = _jump_ss(b, "call_ss")
            else:
                return _unsupported(b, f"unknown Jump/Call sub-command {sub}")
            guard = _compare_guard(b, 6, "byte")
            return _wrap(guard, inner), "ok"
        # Link
        if sub == 0:
            inner = {"op": "nop"}
        elif sub == 1:
            inner = _link_subset(b)
            if inner is None:
                return _unsupported(b, "Link subset with unknown link code")
        elif sub == 4:
            inner = {"op": "link_pgc", "pgc": _u16(b, 6) & 0x7FFF}
        elif sub == 5:
            inner = {"op": "link_ptt", "chapter": _u16(b, 6) & 0x03FF}
            if b[6] >> 2:
                inner["highlight_button"] = b[6] >> 2
        elif sub == 6:
            inner = {"op": "link_program", "program": b[7] & 0x7F}
            if b[6] >> 2:
                inner["highlight_button"] = b[6] >> 2
        elif sub == 7:
            inner = {"op": "link_cell", "cell": b[7]}
            if b[6] >> 2:
                inner["highlight_button"] = b[6] >> 2
        else:
            return _unsupported(b, f"unknown Link sub-command {sub}")
        guard = _compare_guard(b, 3, "imm16")
        return _wrap(guard, inner), "ok"

    if family == 2:
        obj, _status = _decode_type2(b)
        return obj, "ok" if obj.get("op") != "unsupported" else "unsupported"

    if family == 3:
        obj, _status = _decode_type3(b)
        return obj, "ok" if obj.get("op") != "unsupported" else "unsupported"

    return _unsupported(b, f"unknown command family {family}")  # pragma: no cover


def decode_command_list(raw_list, location: str):
    """Decode a list of raw 8-byte commands.

    Returns (objects, unsupported, partial) where unsupported /
    partial are lists of {"raw_hex", "location", "index", "note"}
    records.  Every input command produces exactly one output
    object, in order -- nothing is dropped.
    """
    objects = []
    unsupported = []
    partial = []
    for i, raw in enumerate(raw_list):
        obj, status = decode_command(raw)
        objects.append(obj)
        loc = f"{location}[{i}]"
        if status == "unsupported":
            unsupported.append({"raw_hex": bytes(raw).hex(),
                                "location": loc,
                                "note": obj.get("note", "")})
        elif status == "partial":
            partial.append({"raw_hex": bytes(raw).hex(),
                            "location": loc,
                            "note": obj.get("note", "")})
    return objects, unsupported, partial
