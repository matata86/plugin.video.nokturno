"""QR kód v čistém Pythonu (bajtový režim, oprava chyb M, verze 1–10) a PNG bez PIL.

Kvůli „Nastavit z mobilu“ (`remote_setup.py`): TV ukáže QR s místní adresou.
Doplněk `script.module.qrcode` z repozitáře Kodi potřebuje k obrázku PIL, binární
modul, na který se na Androidu s Pythonem 3.11 nedá spolehnout. Adresa má pár
desítek znaků, takže stačí verze do 10 (až 213 bajtů).

Rozvržení matice kopíruje referenční `python-qrcode`, testy ho s ní porovnávají
modul po modulu při stejné masce a navíc čtou výsledek dekodérem zbar.
"""
import struct
import zlib

# verze → (EC kódových slov na blok, [(počet bloků, datových slov v bloku), …]) pro úroveň M
RS_BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}
ECL_M_BITS = 0b00
MASKS = [
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i * j) % 3 + (i + j) % 2) % 2 == 0,
]

# GF(256) s polynomem 0x11D
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_remainder(data, degree):
    gen = [1]
    for i in range(degree):
        nxt = [0] * (len(gen) + 1)
        for j, coef in enumerate(gen):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _EXP[i])
        gen = nxt
    rem = list(data) + [0] * degree
    for i in range(len(data)):
        factor = rem[i]
        if factor:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gf_mul(gen[j], factor)
    return rem[len(data):]


def _bch(value, poly, poly_bits):
    shifted = value << (poly_bits - 1)
    while shifted.bit_length() >= poly_bits:
        shifted ^= poly << (shifted.bit_length() - poly_bits)
    return (value << (poly_bits - 1)) | shifted


def _capacity(version):
    _ec, blocks = RS_BLOCKS_M[version]
    return sum(n * d for n, d in blocks)


def _codewords(data, version):
    count_bits = 8 if version < 10 else 16
    bits = []

    def put(value, length):
        bits.extend((value >> (length - 1 - k)) & 1 for k in range(length))
    put(0b0100, 4)
    put(len(data), count_bits)
    for byte in data:
        put(byte, 8)
    limit = _capacity(version) * 8
    bits.extend([0] * min(4, limit - len(bits)))
    bits.extend([0] * ((8 - len(bits) % 8) % 8))
    words = [int("".join(map(str, bits[k:k + 8])), 2) for k in range(0, len(bits), 8)]
    pad = 0
    while len(words) < limit // 8:
        words.append(0xEC if pad % 2 == 0 else 0x11)
        pad += 1
    ec_len, blocks = RS_BLOCKS_M[version]
    data_blocks, ec_blocks, offset = [], [], 0
    for count, size in blocks:
        for _ in range(count):
            block = words[offset:offset + size]
            offset += size
            data_blocks.append(block)
            ec_blocks.append(_rs_remainder(block, ec_len))
    out = []
    for k in range(max(len(b) for b in data_blocks)):
        out.extend(b[k] for b in data_blocks if k < len(b))
    for k in range(ec_len):
        out.extend(b[k] for b in ec_blocks)
    return out


def _base(version, mask):
    size = version * 4 + 17
    m = [[None] * size for _ in range(size)]
    for row, col in ((0, 0), (size - 7, 0), (0, size - 7)):
        for r in range(-1, 8):
            for c in range(-1, 8):
                if 0 <= row + r < size and 0 <= col + c < size:
                    m[row + r][col + c] = ((0 <= r <= 6 and c in (0, 6)) or (0 <= c <= 6 and r in (0, 6))
                                           or (2 <= r <= 4 and 2 <= c <= 4))
    positions = ALIGNMENT[version]
    for row in positions:
        for col in positions:
            if m[row][col] is not None:
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    m[row + r][col + c] = r in (-2, 2) or c in (-2, 2) or (r == 0 and c == 0)
    for k in range(8, size - 8):
        if m[k][6] is None:
            m[k][6] = k % 2 == 0
        if m[6][k] is None:
            m[6][k] = k % 2 == 0
    fmt = _bch((ECL_M_BITS << 3) | mask, 0x537, 11) ^ 0x5412
    for i in range(15):
        dark = (fmt >> i) & 1 == 1
        if i < 6:
            m[i][8] = dark
        elif i < 8:
            m[i + 1][8] = dark
        else:
            m[size - 15 + i][8] = dark
        if i < 8:
            m[8][size - i - 1] = dark
        elif i < 9:
            m[8][15 - i] = dark
        else:
            m[8][14 - i] = dark
    m[size - 8][8] = True
    if version >= 7:
        ver = _bch(version, 0x1F25, 13)
        for i in range(18):
            dark = (ver >> i) & 1 == 1
            m[i // 3][i % 3 + size - 11] = dark
            m[i % 3 + size - 11][i // 3] = dark
    return m


def _place(m, codewords, mask):
    size = len(m)
    func = MASKS[mask]
    inc, row, bit, index = -1, size - 1, 7, 0
    for col in range(size - 1, 0, -2):
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if m[row][c] is None:
                    dark = index < len(codewords) and (codewords[index] >> bit) & 1 == 1
                    if func(row, c):
                        dark = not dark
                    m[row][c] = dark
                    bit -= 1
                    if bit == -1:
                        index += 1
                        bit = 7
            row += inc
            if row < 0 or row >= size:
                row -= inc
                inc = -inc
                break
    return m


def _penalty(m):
    size = len(m)
    score = 0
    lines = [row for row in m] + [[m[r][c] for r in range(size)] for c in range(size)]
    for line in lines:
        run = 1
        for k in range(1, size):
            if line[k] == line[k - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
        text = "".join("1" if v else "0" for v in line)
        score += 40 * (text.count("10111010000") + text.count("00001011101"))
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r + 1][c] == m[r][c + 1] == m[r + 1][c + 1]:
                score += 3
    dark = sum(v for row in m for v in row)
    score += abs(dark * 20 // (size * size) - 10) * 10
    return score


def encode(text, mask=None):
    """Matice QR (seznam řádků s bool, True = tmavý) pro text v UTF-8.

    `mask` jen pro testy; jinak se vybere maska s nejmenší penalizací."""
    data = text.encode("utf-8")
    for version in range(1, 11):
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= _capacity(version) * 8:
            break
    else:
        raise ValueError("text je na QR kód verze 10 příliš dlouhý")
    words = _codewords(data, version)
    if mask is not None:
        return _place(_base(version, mask), words, mask)
    return min((_place(_base(version, k), words, k) for k in range(8)), key=_penalty)


def to_png(matrix, scale=10, border=4):
    """PNG (bajty) — černobílý, 8bitová šeď, bez závislostí."""
    size = (len(matrix) + 2 * border) * scale
    rows = []
    blank = b"\x00" + b"\xff" * size
    for _ in range(border * scale):
        rows.append(blank)
    for line in matrix:
        pixels = b"\xff" * (border * scale)
        for dark in line:
            pixels += (b"\x00" if dark else b"\xff") * scale
        pixels += b"\xff" * (border * scale)
        rows.extend([b"\x00" + pixels] * scale)
    for _ in range(border * scale):
        rows.append(blank)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
