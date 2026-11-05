"""Minimal QR Code encoder (byte mode, error correction M, versions 1-10). Stdlib only.

make(text) -> list of rows, each a list of 0/1 (1 = dark), without the quiet zone.
Follows ISO/IEC 18004; the structure mirrors the well-known reference implementations.
"""

# version -> (ec codewords per block, [(block count, data codewords per block), ...])
M_TABLE = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
         8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}

EXP, LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11d
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gmul(a, b):
    return 0 if not a or not b else EXP[LOG[a] + LOG[b]]


def rs_generator(n):
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= gmul(c, 1)
            nxt[j + 1] ^= gmul(c, EXP[i])
        g = nxt
    return g  # highest degree first


def rs_remainder(data, n):
    gen = rs_generator(n)
    rem = [0] * n
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(n):
            rem[i] ^= gmul(gen[i + 1], factor)
    return rem


def capacity(version):
    ec, groups = M_TABLE[version]
    return sum(n * d for n, d in groups)


def codewords(data, version):
    cap = capacity(version)
    bits = '0100' + format(len(data), '08b' if version < 10 else '016b')
    bits += ''.join(format(b, '08b') for b in data)
    bits += '0' * min(4, cap * 8 - len(bits))
    bits += '0' * (-len(bits) % 8)
    cw = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(cw) < cap:
        cw.append(pad[(len(cw) - len(bits) // 8) % 2])
    ec, groups = M_TABLE[version]
    blocks, pos = [], 0
    for n, d in groups:
        for _ in range(n):
            blocks.append(cw[pos:pos + d])
            pos += d
    ecs = [rs_remainder(b, ec) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec):
        for e in ecs:
            out.append(e[i])
    return out


def penalty(m):
    n = len(m)
    score = 0
    for grid in (m, [list(c) for c in zip(*m)]):
        for row in grid:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
            s = ''.join(map(str, row))
            score += 40 * (s.count('10111010000') + s.count('00001011101'))
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return score


def make(text):
    data = text.encode('utf-8')
    version = next((v for v in range(1, 11) if len(data) <= capacity(v) - (2 if v < 10 else 3)), None)
    if version is None:
        raise ValueError('text too long for this encoder')
    size = 17 + 4 * version
    mod = [[0] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            mod[y][x] = 1 if dark else 0
            func[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                setf(cx + dx, cy + dy, d not in (2, 4))
    pos = ALIGN[version]
    for i, cy in enumerate(pos):
        for j, cx in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mask):
        d = mask  # error correction M is 0b00
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        return ((d << 10) | rem) ^ 0x5412

    def draw_format(mask):
        b = format_bits(mask)
        bit = lambda i: ((b >> i) & 1) == 1
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)

    draw_format(0)  # reserve the format area before placing data
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1f25)
        bits = (version << 12) | rem
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            v = ((bits >> i) & 1) == 1
            setf(a, b, v)
            setf(b, a, v)

    cw = codewords(data, version)
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = size - 1 - vert if up else vert
                if not func[y][x] and i < len(cw) * 8:
                    mod[y][x] = (cw[i >> 3] >> (7 - (i & 7))) & 1
                    i += 1
        right -= 2

    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]
    best = None
    for k, fn in enumerate(masks):
        cand = [row[:] for row in mod]
        for y in range(size):
            for x in range(size):
                if not func[y][x] and fn(x, y):
                    cand[y][x] ^= 1
        score = penalty(cand)
        if best is None or score < best[0]:
            best = (score, k, cand)
    _, k, result = best
    mod[:] = result
    draw_format(k)
    return mod
