"""Generate and independently verify small, fictional ZIP64 regression fixtures.

Python's ZIP64 size threshold is lowered only while writing these small files.
The resulting files contain genuine ZIP64 records, not edited classic footers.
Run with --check in CI to verify the committed bytes without rewriting them.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import zipfile


ROOT = Path(__file__).resolve().parent / "fixtures"
POSTS = {
    "archive/data/tweets.js": 'window.YTD.tweets.part0 = [{"tweet":{"id_str":"123456789","full_text":"ZIP64 公开帖子"}}];',
    "archive/data/tweets-part1.js": 'window.YTD.tweets.part1 = [{"tweet":{"id_str":"123456790","full_text":"第二个分片"}}];',
}
MEDIA_NAME = "archive/data/tweets_media/123456789-fixture.jpg"
MEDIA = b"fictional ZIP64 media bytes, not an actual image"
FILES = {**{name: body.encode("utf-8") for name, body in POSTS.items()},
         MEDIA_NAME: MEDIA,
         "archive/data/direct-messages.js": b"FICTIONAL_PRIVATE_ENTRY_MUST_NOT_BE_PARSED"}
VARIANTS = (
    ("zip64-stored.zip", zipfile.ZIP_STORED, False, True),
    ("zip64-deflate.zip", zipfile.ZIP_DEFLATED, False, True),
    ("zip64-streaming.zip", zipfile.ZIP_DEFLATED, True, True),
    ("zip64-local-only.zip", zipfile.ZIP_DEFLATED, False, False),
)


class StreamingBuffer(io.BytesIO):
    def seek(self, *args, **kwargs):
        raise io.UnsupportedOperation("fixture writes ZIP data descriptors")


def generate(method, streaming, full_zip64):
    output = StreamingBuffer() if streaming else io.BytesIO()
    original_limit = zipfile.ZIP64_LIMIT
    try:
        if full_zip64:
            zipfile.ZIP64_LIMIT = 0
        with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
            for name, body in FILES.items():
                entry = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                entry.compress_type = method
                with archive.open(entry, "w", force_zip64=True) as target:
                    target.write(body)
    finally:
        zipfile.ZIP64_LIMIT = original_limit
    return output.getvalue()


def verify(path, method, streaming, full_zip64):
    data = path.read_bytes()
    end_at = data.rfind(b"PK\x05\x06")
    assert end_at == len(data) - 22, f"{path.name}: missing classic EOCD"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == list(FILES)
        assert archive.testzip() is None
        for entry in archive.infolist():
            assert archive.read(entry) == FILES[entry.filename]
            assert entry.compress_type == method
            assert bool(entry.flag_bits & 8) == streaming
            local_at = entry.header_offset
            assert data[local_at:local_at + 4] == b"PK\x03\x04"
            assert struct.unpack_from("<II", data, local_at + 18) == (0xFFFFFFFF, 0xFFFFFFFF)
            name_length, extra_length = struct.unpack_from("<HH", data, local_at + 26)
            extra_at = local_at + 30 + name_length
            assert extra_length >= 20
            assert struct.unpack_from("<HH", data, extra_at) == (1, 16)
    if full_zip64:
        locator_at = end_at - 20
        assert data[locator_at:locator_at + 4] == b"PK\x06\x07"
        disk, eocd64_at, disks = struct.unpack_from("<IQI", data, locator_at + 4)
        assert (disk, disks) == (0, 1)
        assert data[eocd64_at:eocd64_at + 4] == b"PK\x06\x06"
        assert struct.unpack_from("<Q", data, eocd64_at + 4)[0] == 44
        assert struct.unpack_from("<IIQQ", data, eocd64_at + 16) == (0, 0, len(FILES), len(FILES))
        directory_size, directory_at = struct.unpack_from("<QQ", data, eocd64_at + 40)
        assert directory_at + directory_size == eocd64_at
    else:
        assert data[end_at - 20:end_at - 16] != b"PK\x06\x07"
        assert b"PK\x06\x06" not in data
        directory_size, directory_at = struct.unpack_from("<II", data, end_at + 12)
        assert directory_at + directory_size == end_at
    central_at = directory_at
    for _ in FILES:
        assert data[central_at:central_at + 4] == b"PK\x01\x02"
        compressed_size, size = struct.unpack_from("<II", data, central_at + 20)
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", data, central_at + 28)
        extra_at = central_at + 46 + name_length
        if full_zip64:
            assert (compressed_size, size) == (0xFFFFFFFF, 0xFFFFFFFF)
            assert struct.unpack_from("<H", data, extra_at)[0] == 1
            assert extra_length in (20, 28)
        else:
            assert max(compressed_size, size) < 0xFFFFFFFF and extra_length == 0
        central_at = extra_at + extra_length + comment_length
    assert central_at == directory_at + directory_size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = {"source": "Python stdlib zipfile, fictional data only",
                "fixtures": [variant[0] for variant in VARIANTS],
                "posts": [{"name": name, "text": body} for name, body in POSTS.items()],
                "media_name": MEDIA_NAME.rsplit("/", 1)[1],
                "media_bytes": len(MEDIA), "media_sha256": hashlib.sha256(MEDIA).hexdigest()}
    if not args.check:
        ROOT.mkdir(exist_ok=True)
        for name, method, streaming, full_zip64 in VARIANTS:
            (ROOT / name).write_bytes(generate(method, streaming, full_zip64))
        (ROOT / "zip64-manifest.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert json.loads((ROOT / "zip64-manifest.json").read_text(encoding="utf-8")) == expected
    for name, method, streaming, full_zip64 in VARIANTS:
        verify(ROOT / name, method, streaming, full_zip64)
    print(f"Verified {len(VARIANTS)} genuine ZIP64 fixtures with Python zipfile and binary record checks.")


if __name__ == "__main__":
    main()
