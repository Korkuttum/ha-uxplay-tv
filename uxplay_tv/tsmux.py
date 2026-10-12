"""Küçük MPEG-TS paketleyici: H.264 (Annex B) video + AAC (ADTS) ses.

ffmpeg'e bağımlı olmadan, seyrek gelen (sabit ekran) videoda da çalışır.
"""
import struct
import time

SC = b"\x00\x00\x00\x01"
PID_PMT, PID_VIDEO, PID_AUDIO = 0x1000, 0x100, 0x101
PSI_INTERVAL = 0.1  # sn


def _make_crc_table():
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return table


_CRC = _make_crc_table()


def crc32_mpeg(data):
    c = 0xFFFFFFFF
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC[((c >> 24) ^ b) & 0xFF]
    return c


def _section(table_id, ext_id, body):
    length = 5 + len(body) + 4
    sec = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF]) + struct.pack(">H", ext_id) \
        + bytes([0xC1, 0, 0]) + body
    return sec + struct.pack(">I", crc32_mpeg(sec))


PAT = _section(0x00, 1, struct.pack(">HH", 1, 0xE000 | PID_PMT))
PMT = _section(
    0x02, 1,
    struct.pack(">HH", 0xE000 | PID_AUDIO, 0xF000)                       # PCR pid = ses (sürekli akar)
    + bytes([0x1B]) + struct.pack(">HH", 0xE000 | PID_VIDEO, 0xF000)     # H.264
    + bytes([0x0F]) + struct.pack(">HH", 0xE000 | PID_AUDIO, 0xF000),    # AAC (ADTS)
)


def _ts_ts(value):
    """33 bit PTS -> 5 bayt (PTS-only işaretiyle)."""
    return bytes([
        0x21 | ((value >> 29) & 0x0E),
        (value >> 22) & 0xFF,
        0x01 | ((value >> 14) & 0xFE),
        (value >> 7) & 0xFF,
        0x01 | ((value << 1) & 0xFE),
    ])


def _pcr_bytes(pcr27):
    base, ext = divmod(pcr27, 300)
    base &= (1 << 33) - 1
    return bytes([
        (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
        ((base & 1) << 7) | 0x7E | (ext >> 8), ext & 0xFF,
    ])


class TsMuxer:
    def __init__(self, emit):
        self.emit = emit
        self.cc = {}
        self._psi_at = 0.0

    def _next_cc(self, pid):
        c = self.cc.get(pid, 0)
        self.cc[pid] = (c + 1) & 15
        return c

    def _packets(self, pid, data, pcr27=None, random_access=False):
        out = bytearray()
        pos, first = 0, True
        while first or pos < len(data):
            remaining = len(data) - pos
            flags = b""
            if first and (pcr27 is not None or random_access):
                f = (0x10 if pcr27 is not None else 0) | (0x40 if random_access else 0)
                flags = bytes([f]) + (_pcr_bytes(pcr27) if pcr27 is not None else b"")
            room = 184 - (1 + len(flags) if flags else 0)
            take = min(remaining, room)
            need = 184 - take  # uyarlama alanı için gereken bayt (uzunluk baytı dahil)
            if need == 0:
                af = b""
            elif need == 1:
                af = b"\x00"
            else:
                body = flags if flags else b"\x00"
                af = bytes([need - 1]) + body + b"\xff" * (need - 1 - len(body))
            out += bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF,
                          (0x30 if af else 0x10) | self._next_cc(pid)]) + af + data[pos:pos + take]
            pos += take
            first = False
        return bytes(out)

    def psi(self, force=False):
        now = time.monotonic()
        if not force and now - self._psi_at < PSI_INTERVAL:
            return b""
        self._psi_at = now
        pat = self._packets(0, b"\x00" + PAT + b"\xff" * (183 - len(PAT)))
        pmt = self._packets(PID_PMT, b"\x00" + PMT + b"\xff" * (183 - len(PMT)))
        return pat + pmt

    def video(self, au, pts90, key=False):
        pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + _ts_ts(pts90 & ((1 << 33) - 1)) + au
        self.emit(self.psi(force=key) + self._packets(PID_VIDEO, pes, random_access=key))

    def audio(self, frame, pts90, pcr27):
        pes = b"\x00\x00\x01\xc0" + struct.pack(">H", 8 + len(frame)) + b"\x80\x80\x05" \
            + _ts_ts(pts90 & ((1 << 33) - 1)) + frame
        self.emit(self.psi() + self._packets(PID_AUDIO, pes, pcr27=pcr27))


class H264Depacketizer:
    """RTP (RFC 6184) -> erişim birimi (Annex B)."""

    def __init__(self):
        self.nals, self.fu, self.ts = [], None, None
        self.sps = self.pps = None

    def feed(self, pkt):
        """Paket bir kareyi tamamlarsa (au_bytes, rtp_ts, is_key) döner."""
        if len(pkt) < 13:
            return None
        hdr = 12 + 4 * (pkt[0] & 0x0F)
        if pkt[0] & 0x10 and len(pkt) >= hdr + 4:  # başlık uzantısı
            hdr += 4 + 4 * int.from_bytes(pkt[hdr + 2:hdr + 4], "big")
        payload = pkt[hdr:]
        if not payload:
            return None
        if self.ts is None:
            self.ts = int.from_bytes(pkt[4:8], "big")
        t = payload[0] & 0x1F
        if 1 <= t <= 23:
            self.nals.append(bytes(payload))
        elif t == 24:  # STAP-A
            i = 1
            while i + 2 <= len(payload):
                n = int.from_bytes(payload[i:i + 2], "big")
                self.nals.append(bytes(payload[i + 2:i + 2 + n]))
                i += 2 + n
        elif t == 28 and len(payload) > 2:  # FU-A
            fu = payload[1]
            if fu & 0x80:
                self.fu = bytearray([(payload[0] & 0xE0) | (fu & 0x1F)])
            if self.fu is not None:
                self.fu += payload[2:]
                if fu & 0x40:
                    self.nals.append(bytes(self.fu))
                    self.fu = None
        if not pkt[1] & 0x80:  # marker yok: kare sürüyor
            return None
        nals, ts = self.nals, self.ts
        self.nals, self.fu, self.ts = [], None, None
        return self._build(nals, ts)

    def _build(self, nals, ts):
        # SPS'e "yeniden sıralama yok" bildirimi ekle (sabit ekranda görüntü gecikmesin)
        nals = [add_bitstream_restriction(n) if n and n[0] & 0x1F == 7 else n for n in nals]
        types = [n[0] & 0x1F for n in nals if n]
        for n in nals:
            if n and n[0] & 0x1F == 7:
                self.sps = n
            elif n and n[0] & 0x1F == 8:
                self.pps = n
        key = 5 in types
        if not any(t in (1, 5) for t in types):
            return None  # yalnızca SPS/PPS vb.: kare yok
        out = bytearray(SC + b"\x09\xf0")  # erişim birimi ayırıcı
        if key and (7 not in types or 8 not in types) and self.sps and self.pps:
            out += SC + self.sps + SC + self.pps
        for n in nals:
            if n and n[0] & 0x1F != 9:
                out += SC + n
        return bytes(out), ts, key


# ---------------------------------------------------------------------------
# SPS yeniden yazma: "yeniden sıralama yok" bildirimi ekle
#
# iPhone'un SPS'inde bitstream_restriction yok. Böyle akışlarda çözücüler seviyenin izin
# verdiği en kötü durumu (burada 8 kare) varsayıp çıktıyı geciktirir; ekran sabitken (saniyede
# birkaç kare) görüntü bir türlü gelmez. num_reorder_frames=0 ile çözücü kareyi hemen gösterir.
# ---------------------------------------------------------------------------

def _unescape(b):
    out, zeros = bytearray(), 0
    for x in b:
        if zeros >= 2 and x == 3:
            zeros = 0
            continue
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


def _escape(b):
    out, zeros = bytearray(), 0
    for x in b:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


class _BitReader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def bit(self):
        v = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return v

    def bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self):
        zeros = 0
        while self.bit() == 0:
            zeros += 1
        return (1 << zeros) - 1 + (self.bits(zeros) if zeros else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _write_ue(out, value):
    value += 1
    n = value.bit_length()
    out.extend([0] * (n - 1))
    out.extend((value >> i) & 1 for i in range(n - 1, -1, -1))


def _skip_hrd(r):
    n = r.ue()
    r.bits(8)
    for _ in range(n + 1):
        r.ue()
        r.ue()
        r.bit()
    r.bits(20)


def add_bitstream_restriction(sps):
    """SPS NAL'ine (başlık baytı dahil) num_reorder_frames=0 olan bitstream_restriction ekler.

    Çözümleyemezse veya zaten ayarlıysa özgün SPS'i döndürür."""
    try:
        rbsp = _unescape(sps[1:])
        r = _BitReader(rbsp)
        profile = r.bits(8)
        r.bits(16)
        r.ue()  # sps_id
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = r.ue()
            if chroma == 3:
                r.bit()
            r.ue()
            r.ue()
            r.bit()
            if r.bit():  # ölçekleme matrisleri
                for i in range(8 if chroma != 3 else 12):
                    if r.bit():
                        last = nxt = 8
                        for _ in range(16 if i < 6 else 64):
                            if nxt != 0:
                                nxt = (last + r.se() + 256) % 256
                            last = last if nxt == 0 else nxt
        r.ue()  # log2_max_frame_num
        poc = r.ue()
        if poc == 0:
            r.ue()
        elif poc == 1:
            r.bit()
            r.se()
            r.se()
            for _ in range(r.ue()):
                r.se()
        refs = r.ue()
        r.bit()
        r.ue()
        r.ue()
        if not r.bit():  # frame_mbs_only_flag
            r.bit()
        r.bit()
        if r.bit():  # kırpma
            for _ in range(4):
                r.ue()
        vui_flag_pos = r.pos
        has_vui = r.bit()
        cut = vui_flag_pos  # buraya kadar aynen kopyalanır
        if has_vui:
            if r.bit() and r.bits(8) == 255:
                r.bits(32)
            if r.bit():
                r.bit()
            if r.bit():
                r.bits(4)
                if r.bit():
                    r.bits(24)
            if r.bit():
                r.ue()
                r.ue()
            if r.bit():
                r.bits(64)
                r.bit()
            hrd = 0
            for _ in range(2):
                if r.bit():
                    hrd = 1
                    _skip_hrd(r)
            if hrd:
                r.bit()
            r.bit()  # pic_struct_present_flag
            cut = r.pos  # bitstream_restriction_flag burada
            if r.bit():
                return sps  # zaten var
        bits = [(rbsp[i >> 3] >> (7 - (i & 7))) & 1 for i in range(cut)]
        if not has_vui:
            bits.append(1)  # vui_parameters_present_flag
            bits.extend([0] * 8)  # aspect, overscan, video_signal, chroma_loc, timing, nal_hrd, vcl_hrd, pic_struct
        bits.append(1)  # bitstream_restriction_flag
        bits.append(1)  # motion_vectors_over_pic_boundaries_flag
        for v in (0, 0, 15, 15, 0, max(refs, 1)):
            _write_ue(bits, v)  # denomlar, mv uzunlukları, num_reorder_frames=0, max_dec_frame_buffering
        bits.append(1)  # rbsp_stop_one_bit
        bits.extend([0] * (-len(bits) % 8))
        data = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
        return sps[:1] + _escape(data)
    except (IndexError, ValueError):
        return sps
