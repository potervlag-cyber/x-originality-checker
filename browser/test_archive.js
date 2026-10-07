'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {deflateRawSync} = require('node:zlib');
const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
require('./archive.js');

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc & 1) ? 0xedb88320 ^ (crc >>> 1) : crc >>> 1;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

// A valid classic ZIP fixture. Padding emulates large, irrelevant archived media
// without allocating 300 MB during the test.
function zipFixture(items, options = {}) {
  const sections = [], central = [];
  let offset = 0;
  for (const item of items) {
    const name = Buffer.from(item.name), content = Buffer.from(item.content || '');
    const method = item.method ?? 8, compressed = method === 8 ? deflateRawSync(content) : content;
    const flags = item.flags ?? 0x800;
    const size = item.claimSize ?? content.length, digest = item.crc ?? crc32(content);
    const header = Buffer.alloc(30 + name.length);
    header.writeUInt32LE(0x04034b50); header.writeUInt16LE(20, 4);
    header.writeUInt16LE(flags, 6); header.writeUInt16LE(method, 8);
    header.writeUInt32LE(digest, 14); header.writeUInt32LE(compressed.length, 18); header.writeUInt32LE(size, 22);
    header.writeUInt16LE(name.length, 26); name.copy(header, 30);
    const centralComment = Buffer.from(item.centralComment || '');
    const directory = Buffer.alloc(46 + name.length + centralComment.length);
    directory.writeUInt32LE(0x02014b50); directory.writeUInt16LE(20, 4); directory.writeUInt16LE(20, 6);
    directory.writeUInt16LE(flags, 8); directory.writeUInt16LE(method, 10); directory.writeUInt32LE(digest, 16);
    directory.writeUInt32LE(compressed.length, 20); directory.writeUInt32LE(size, 24); directory.writeUInt16LE(name.length, 28);
    directory.writeUInt16LE(centralComment.length, 32);
    directory.writeUInt32LE(offset, 42); name.copy(directory, 46); centralComment.copy(directory, 46 + name.length);
    sections.push({at: offset, bytes: header}, {at: offset + header.length, bytes: compressed});
    central.push(directory);
    offset += header.length + compressed.length;
  }
  const directory = Buffer.concat(central), comment = Buffer.from(options.comment || '');
  const footer = Buffer.alloc(22 + comment.length);
  const directoryAt = options.totalSize ? options.totalSize - directory.length - footer.length : offset;
  footer.writeUInt32LE(0x06054b50); footer.writeUInt16LE(items.length, 8); footer.writeUInt16LE(items.length, 10);
  footer.writeUInt32LE(directory.length, 12); footer.writeUInt32LE(directoryAt, 16); footer.writeUInt16LE(comment.length, 20);
  comment.copy(footer, 22);
  sections.push({at: directoryAt, bytes: directory}, {at: directoryAt + directory.length, bytes: footer});
  const size = directoryAt + directory.length + footer.length;
  return sparseZip(sections, size);
}

function sparseZip(sections, size) {
  const reads = [];
  return {
    size, reads, sections,
    slice(start = 0, end = size) {
      end = Math.min(end, size);
      start = Math.max(start, 0);
      reads.push([start, end]);
      const result = Buffer.alloc(Math.max(0, end - start));
      for (const section of sections) {
        const left = Math.max(start, section.at), right = Math.min(end, section.at + section.bytes.length);
        if (left < right) section.bytes.copy(result, left - start, left - section.at, right - section.at);
      }
      return new Blob([result]);
    },
    arrayBuffer() { throw new Error('The complete ZIP must never be read'); }
  };
}

function extraField(tag, payload) {
  const field = Buffer.alloc(4 + payload.length);
  field.writeUInt16LE(tag); field.writeUInt16LE(payload.length, 2); payload.copy(field, 4);
  return field;
}

function uint64s(values) {
  const bytes = Buffer.alloc(values.length * 8);
  values.forEach((value, index) => bytes.writeBigUInt64LE(BigInt(value), index * 8));
  return bytes;
}

// A complete single-disk ZIP64: the locator points to a real ZIP64 EOCD,
// and each central extra contains only fields whose classic value is a sentinel.
// Small payloads keep the tests fast; ZIP64 is a structure, not a file-size label.
function zip64Fixture(items, options = {}) {
  const sections = [], centralHeaders = [], localHeaders = [];
  let offset = 0;
  for (const item of items) {
    const name = Buffer.from(item.name), content = Buffer.from(item.content || '');
    const method = item.method ?? 8, compressed = method === 8 ? deflateRawSync(content) : content;
    const flags = item.flags ?? 0x800, size = item.claimSize ?? content.length, digest = item.crc ?? crc32(content);
    const fields = new Set(item.zip64Fields ?? ['size', 'compressedSize', 'localAt']);
    const local64 = item.localZip64 ?? (fields.has('size') || fields.has('compressedSize'));
    const localExtra = local64 ? extraField(1, uint64s([size, compressed.length])) : Buffer.alloc(0);
    const header = Buffer.alloc(30 + name.length + localExtra.length);
    header.writeUInt32LE(0x04034b50); header.writeUInt16LE(local64 ? 45 : 20, 4);
    header.writeUInt16LE(flags, 6); header.writeUInt16LE(method, 8);
    header.writeUInt32LE(flags & 8 ? 0 : digest, 14);
    header.writeUInt32LE(local64 ? 0xffffffff : flags & 8 ? 0 : compressed.length, 18);
    header.writeUInt32LE(local64 ? 0xffffffff : flags & 8 ? 0 : size, 22);
    header.writeUInt16LE(name.length, 26); header.writeUInt16LE(localExtra.length, 28);
    name.copy(header, 30); localExtra.copy(header, 30 + name.length);
    const values = [];
    for (const [field, value] of [['size', size], ['compressedSize', compressed.length], ['localAt', offset]]) {
      if (fields.has(field)) values.push(uint64s([value]));
    }
    if (fields.has('startDisk')) { const disk = Buffer.alloc(4); disk.writeUInt32LE(item.startDisk ?? 0); values.push(disk); }
    const zip64Extra = fields.size ? extraField(1, Buffer.concat(values)) : Buffer.alloc(0);
    const centralExtra = Buffer.concat([item.prefixExtra || Buffer.alloc(0), zip64Extra]);
    const directory = Buffer.alloc(46 + name.length + centralExtra.length);
    directory.writeUInt32LE(0x02014b50); directory.writeUInt16LE(45, 4); directory.writeUInt16LE(45, 6);
    directory.writeUInt16LE(flags, 8); directory.writeUInt16LE(method, 10); directory.writeUInt32LE(digest, 16);
    directory.writeUInt32LE(fields.has('compressedSize') ? 0xffffffff : compressed.length, 20);
    directory.writeUInt32LE(fields.has('size') ? 0xffffffff : size, 24);
    directory.writeUInt16LE(name.length, 28); directory.writeUInt16LE(centralExtra.length, 30);
    directory.writeUInt16LE(fields.has('startDisk') ? 0xffff : item.startDisk ?? 0, 34);
    directory.writeUInt32LE(fields.has('localAt') ? 0xffffffff : offset, 42);
    name.copy(directory, 46); centralExtra.copy(directory, 46 + name.length);
    sections.push({at: offset, bytes: header}, {at: offset + header.length, bytes: compressed});
    localHeaders.push(header); centralHeaders.push(directory);
    offset += header.length + compressed.length;
    if (flags & 8) {
      const descriptor = Buffer.alloc(local64 ? 24 : 16);
      descriptor.writeUInt32LE(0x08074b50); descriptor.writeUInt32LE(digest, 4);
      if (local64) { descriptor.writeBigUInt64LE(BigInt(compressed.length), 8); descriptor.writeBigUInt64LE(BigInt(size), 16); }
      else { descriptor.writeUInt32LE(compressed.length, 8); descriptor.writeUInt32LE(size, 12); }
      sections.push({at: offset, bytes: descriptor}); offset += descriptor.length;
    }
  }
  const directory = Buffer.concat(centralHeaders), comment = Buffer.from(options.comment || '');
  let centralAt = 0;
  centralHeaders.forEach((header, index) => {
    centralHeaders[index] = directory.subarray(centralAt, centralAt + header.length);
    centralAt += header.length;
  });
  const endExtension = options.endExtension || Buffer.alloc(0);
  const eocd64 = options.zip64Footer === false ? Buffer.alloc(0) : Buffer.alloc(56 + endExtension.length);
  const locator = eocd64.length ? Buffer.alloc(20) : Buffer.alloc(0), footer = Buffer.alloc(22 + comment.length);
  const directoryAt = options.totalSize ? options.totalSize - directory.length - eocd64.length - locator.length - footer.length : offset;
  const directoryEnd = directoryAt + directory.length;
  if (eocd64.length) {
    eocd64.writeUInt32LE(0x06064b50); eocd64.writeBigUInt64LE(BigInt(44 + endExtension.length), 4);
    eocd64.writeUInt16LE(45, 12); eocd64.writeUInt16LE(45, 14);
    eocd64.writeBigUInt64LE(BigInt(items.length), 24); eocd64.writeBigUInt64LE(BigInt(items.length), 32);
    eocd64.writeBigUInt64LE(BigInt(directory.length), 40); eocd64.writeBigUInt64LE(BigInt(directoryAt), 48);
    endExtension.copy(eocd64, 56);
    locator.writeUInt32LE(0x07064b50); locator.writeBigUInt64LE(BigInt(directoryEnd), 8); locator.writeUInt32LE(1, 16);
  }
  const classic = options.classicFields || !eocd64.length;
  footer.writeUInt32LE(0x06054b50);
  footer.writeUInt16LE(classic ? items.length : 0xffff, 8); footer.writeUInt16LE(classic ? items.length : 0xffff, 10);
  footer.writeUInt32LE(classic ? directory.length : 0xffffffff, 12); footer.writeUInt32LE(classic ? directoryAt : 0xffffffff, 16);
  footer.writeUInt16LE(comment.length, 20); comment.copy(footer, 22);
  sections.push({at: directoryAt, bytes: directory}, {at: directoryEnd, bytes: eocd64},
    {at: directoryEnd + eocd64.length, bytes: locator}, {at: directoryEnd + eocd64.length + locator.length, bytes: footer});
  const file = sparseZip(sections, directoryEnd + eocd64.length + locator.length + footer.length);
  return Object.assign(file, {centralHeaders, localHeaders, eocd64, locator, footer, directoryAt});
}

const publicItem = {name: 'archive/data/tweets.js', content: 'window.YTD.tweets.part0 = [{"tweet":{"id_str":"123456789","full_text":"全部公开帖子"}}];'};

test('commentless ZIP reads posts without reading unrelated private entries', async () => {
  const file = zipFixture([publicItem, {name: 'archive/data/direct-messages.js', content: 'private content must not be read'}, {name: 'archive/data/account.js', content: 'private account'}]);
  const reader = await ArchiveZip.open(file);
  const files = await reader.readPostFiles();
  assert.deepEqual(files, [{name: publicItem.name, text: publicItem.content}]);
  assert.equal(reader.metadata.ignored_entries, 2);
  const privateSections = file.sections.slice(2, 6);
  for (const section of privateSections) for (const [left, right] of file.reads) assert.ok(right <= section.at || left >= section.at + section.bytes.length, 'private entry bytes should not be read');
});

test('300 MiB sparse ZIP is accepted through bounded selective Blob slices', async () => {
  const file = zipFixture([publicItem], {totalSize: ArchiveZip.MAX_UPLOAD_BYTES});
  const reader = await ArchiveZip.open(file);
  assert.equal((await reader.readPostFiles())[0].text, publicItem.content);
  assert.equal(reader.metadata.input_bytes, 314572800);
  assert.ok(file.reads.every(([left, right]) => right - left < 1024), 'no whole-file read');
  assert.ok(file.reads.reduce((sum, [left, right]) => sum + right - left, 0) < 2048);
});

test('rejects one byte over 300 MiB before reading any bytes', async () => {
  let sliced = false;
  await assert.rejects(ArchiveZip.open({size: ArchiveZip.MAX_UPLOAD_BYTES + 1, slice() { sliced = true; }}), /300 MB/);
  assert.equal(sliced, false);
});

test('stored and multipart archives include every part in numeric order', async () => {
  const file = zipFixture([
    {name: 'data/posts-part10.js', content: 'ten', method: 0},
    {name: 'data/posts-part2.js', content: 'two', method: 0},
    {name: 'data/posts.js', content: 'zero', method: 0},
    {name: 'data/note-tweet.js', content: 'note', method: 0},
  ]);
  const reader = await ArchiveZip.open(file);
  assert.equal(reader.postEntries.length, 3);
  assert.equal(reader.postEntries[0].name, 'data/posts.js');
  assert.equal(reader.noteEntries.length, 1);
  assert.equal((await reader.readPostFiles({includeNotes: true})).length, 4);
  assert.deepEqual(reader.postEntries.filter(item => item.name.includes('part')).map(item => item.name), ['data/posts-part2.js', 'data/posts-part10.js']);
});

test('stream hashes only referenced media with incremental callbacks', async () => {
  const media = Buffer.alloc(200000, 42);
  const file = zipFixture([publicItem, {name: 'archive/data/tweets_media/123456789-picture.jpg', content: media}, {name: 'archive/data/tweets_media/unrelated.jpg', content: 'ignore me'}]);
  const reader = await ArchiveZip.open(file);
  let updates = 0;
  const result = await reader.hashMedia(['123456789-picture.jpg', 'not-found.jpg'], {createHasher() {
    const hasher = createHash('sha256');
    return {update(chunk) { updates++; hasher.update(chunk); }, digest() { return hasher.digest('hex'); }};
  }});
  assert.deepEqual(result.hashes, [{name: '123456789-picture.jpg', hash: createHash('sha256').update(media).digest('hex'), hash_origin: 'local_file'}]);
  assert.deepEqual(result.missing, ['not-found.jpg']);
  assert.equal(result.hashed_bytes, media.length);
  assert.ok(updates > 1, 'media should be hashed as decompressed chunks');
});

test('CRC corruption is rejected rather than analyzed', async () => {
  const reader = await ArchiveZip.open(zipFixture([{...publicItem, crc: 1234}]));
  await assert.rejects(reader.readPostFiles(), /CRC/);
});

test('declared expansion length mismatch is rejected', async () => {
  const reader = await ArchiveZip.open(zipFixture([{...publicItem, claimSize: 3}]));
  await assert.rejects(reader.readPostFiles(), /解压长度/);
});

test('encrypted public content and unsupported compression give explicit errors', async () => {
  for (const [options, match] of [[{flags: 0x801}, /加密/], [{method: 12}, /压缩方式/]]) {
    const reader = await ArchiveZip.open(zipFixture([{...publicItem, ...options}]));
    await assert.rejects(reader.readPostFiles(), match);
  }
});

test('path traversal and duplicate names are rejected', async () => {
  await assert.rejects(ArchiveZip.open(zipFixture([publicItem, {name: '../secret', content: 'x'}])), /无效路径/);
  await assert.rejects(ArchiveZip.open(zipFixture([publicItem, publicItem])), /重名/);
});

test('incomplete and private-only ZIPs are not accepted as public post archives', async () => {
  await assert.rejects(ArchiveZip.open(new Blob([Buffer.alloc(100)])), /完整目录/);
  await assert.rejects(ArchiveZip.open(zipFixture([{name: 'data/account.js', content: '[]'}])), /未找到/);
});

test('multipart disks are rejected explicitly', async () => {
  const multipart = zipFixture([publicItem]);
  multipart.sections.at(-1).bytes.writeUInt16LE(1, 4);
  await assert.rejects(ArchiveZip.open(multipart), /分卷/);
});

test('ZIP64 fixtures generated by Python zipfile read all posts and hash referenced media', async () => {
  const manifest = JSON.parse(readFileSync(join(__dirname, 'fixtures', 'zip64-manifest.json'), 'utf8'));
  for (const name of manifest.fixtures) {
    const reader = await ArchiveZip.open(new Blob([readFileSync(join(__dirname, 'fixtures', name))]));
    assert.deepEqual(await reader.readPostFiles(), manifest.posts, name);
    assert.equal(reader.metadata.zip_entries, 4, name);
    assert.equal(reader.metadata.ignored_entries, 2, name);
    const result = await reader.hashMedia([manifest.media_name], {createHasher() {
      const hash = createHash('sha256');
      return {update(bytes) { hash.update(bytes); }, digest() { return hash.digest('hex'); }};
    }});
    assert.deepEqual(result.hashes, [{name: manifest.media_name, hash: manifest.media_sha256, hash_origin: 'local_file'}], name);
    assert.equal(result.hashed_bytes, manifest.media_bytes, name);
    assert.deepEqual(result.missing, [], name);
  }
});

test('ZIP64 EOCD and locator work with comments, extensions and non-sentinel classic fields', async () => {
  for (const options of [{}, {comment: 'ZIP64 comment PK\u0005\u0006 extra'},
    {classicFields: true}, {classicFields: true, comment: 'comment'}, {endExtension: Buffer.alloc(32, 42)}]) {
    const file = zip64Fixture([publicItem], options);
    assert.equal(file.eocd64.readUInt32LE(), 0x06064b50);
    assert.equal(file.locator.readUInt32LE(), 0x07064b50);
    assert.equal(file.locator.readBigUInt64LE(8), BigInt(file.directoryAt + file.centralHeaders[0].length));
    assert.deepEqual(await (await ArchiveZip.open(file)).readPostFiles(), [{name: publicItem.name, text: publicItem.content}]);
  }
});

test('ZIP64 central extras decode only sentinel fields in the specified order', async () => {
  for (const zip64Fields of [['size'], ['compressedSize'], ['localAt'], ['startDisk'],
    ['size', 'compressedSize'], ['size', 'localAt'], ['compressedSize', 'localAt'], ['localAt', 'startDisk'],
    ['size', 'compressedSize', 'localAt', 'startDisk'], []]) {
    const file = zip64Fixture([{name: 'unrelated.txt', content: 'padding', method: 0, zip64Fields: [], localZip64: false},
      {...publicItem, zip64Fields, prefixExtra: extraField(0xcafe, Buffer.from([1, 2, 3]))}]);
    const reader = await ArchiveZip.open(file);
    assert.equal(reader.postEntries[0].localAt, file.sections[2].at, zip64Fields.join(','));
    assert.ok(reader.postEntries[0].localAt > 0);
    assert.equal(reader.postEntries[0].size, Buffer.byteLength(publicItem.content), zip64Fields.join(','));
    assert.equal((await reader.readPostFiles())[0].text, publicItem.content, zip64Fields.join(','));
  }
});

test('ZIP64 entry extras and forced ZIP64 local headers work with a classic EOCD', async () => {
  for (const item of [{...publicItem}, {...publicItem, zip64Fields: [], localZip64: true},
    {...publicItem, zip64Fields: [], localZip64: true, flags: 0x808}]) {
    const file = zip64Fixture([item], {zip64Footer: false});
    assert.equal((await (await ArchiveZip.open(file)).readPostFiles())[0].text, publicItem.content);
  }
});

test('ZIP64 preserves selective reads at 300 MiB without reading private payloads', async () => {
  const file = zip64Fixture([publicItem, {name: 'archive/data/direct-messages.js', content: 'FICTIONAL_PRIVATE_ONLY'}],
    {totalSize: ArchiveZip.MAX_UPLOAD_BYTES, classicFields: true});
  const reader = await ArchiveZip.open(file);
  assert.equal((await reader.readPostFiles())[0].text, publicItem.content);
  assert.equal(reader.metadata.input_bytes, 314572800);
  for (const section of file.sections.slice(2, 4)) {
    for (const [left, right] of file.reads) assert.ok(right <= section.at || left >= section.at + section.bytes.length, 'private entry bytes should not be read');
  }
  assert.ok(file.reads.every(([left, right]) => right - left < 1024));
  assert.ok(file.reads.reduce((sum, [left, right]) => sum + right - left, 0) < 2048);
});

test('exactly 65535 classic ZIP entries are not mistaken for a ZIP64 footer', async () => {
  const items = [publicItem, ...Array.from({length: 65534}, (_, index) => ({name: `unused/${index}`, method: 0}))];
  const reader = await ArchiveZip.open(zipFixture(items));
  assert.equal(reader.metadata.zip_entries, 65535);
  assert.equal((await reader.readPostFiles())[0].text, publicItem.content);
});

test('damaged classic sentinel fixture does not stand in for valid ZIP64', async () => {
  const file = zipFixture([publicItem]);
  file.sections.at(-1).bytes.writeUInt16LE(0xffff, 8); file.sections.at(-1).bytes.writeUInt16LE(0xffff, 10);
  await assert.rejects(ArchiveZip.open(file), /ZIP.*目录/);
});

test('ZIP64 refuses missing, truncated and inconsistent end records', async () => {
  const cases = [
    file => file.locator.writeUInt32LE(0),
    file => file.eocd64.writeUInt32LE(0),
    file => file.eocd64.writeBigUInt64LE(43n, 4),
    file => file.eocd64.writeBigUInt64LE(45n, 4),
    file => file.locator.writeBigUInt64LE(BigInt(file.size - 22), 8),
    file => file.eocd64.writeBigUInt64LE(BigInt(file.directoryAt + 1), 48),
  ];
  for (const mutate of cases) {
    const file = zip64Fixture([publicItem]); mutate(file);
    await assert.rejects(ArchiveZip.open(file), /ZIP/);
  }
  const inconsistent = zip64Fixture([publicItem], {classicFields: true});
  inconsistent.footer.writeUInt16LE(2, 8); inconsistent.footer.writeUInt16LE(2, 10);
  await assert.rejects(ArchiveZip.open(inconsistent), /ZIP64.*不一致/);
});

test('ZIP64 central extras reject missing, truncated and duplicated required fields', async () => {
  for (const mutate of [
    (header, extraAt) => header.writeUInt16LE(2, extraAt),
    (header, extraAt) => header.writeUInt16LE(8, extraAt + 2),
    (header, extraAt) => header.writeUInt16LE(1000, extraAt + 2),
  ]) {
    const file = zip64Fixture([publicItem]), header = file.centralHeaders[0];
    mutate(header, 46 + header.readUInt16LE(28));
    await assert.rejects(ArchiveZip.open(file), /ZIP/);
  }
  const duplicate = zip64Fixture([{...publicItem, prefixExtra: extraField(1, uint64s([0]))}]);
  await assert.rejects(ArchiveZip.open(duplicate), /ZIP64.*重复/);
});

test('ZIP64 local extras are validated against the directory before reading content', async () => {
  for (const mutate of [
    (header, extraAt) => header.writeUInt16LE(2, extraAt),
    (header, extraAt) => header.writeUInt16LE(8, extraAt + 2),
    (header, extraAt) => header.writeBigUInt64LE(BigInt(Buffer.byteLength(publicItem.content) + 1), extraAt + 4),
    (header, extraAt) => header.writeBigUInt64LE(9007199254740992n, extraAt + 4),
  ]) {
    const file = zip64Fixture([publicItem]), header = file.localHeaders[0];
    mutate(header, 30 + header.readUInt16LE(26));
    const reader = await ArchiveZip.open(file);
    await assert.rejects(reader.readPostFiles(), /ZIP/);
  }
});

test('ZIP64 rejects uint64 values beyond JavaScript safe integers before oversized reads', async () => {
  const unsafe = 9007199254740992n;
  for (const mutate of [
    file => file.locator.writeBigUInt64LE(unsafe, 8),
    ...[4, 24, 32, 40, 48].map(at => file => file.eocd64.writeBigUInt64LE(unsafe, at)),
    file => { const header = file.centralHeaders[0]; header.writeBigUInt64LE(unsafe, 50 + header.readUInt16LE(28)); },
  ]) {
    const file = zip64Fixture([publicItem]); mutate(file);
    await assert.rejects(ArchiveZip.open(file), /安全整数/);
    assert.ok(file.reads.every(([left, right]) => right - left < 1024));
  }
});

test('ZIP64 keeps entry-count, directory, expanded-text and media resource limits', async () => {
  for (const [at, value] of [[32, 200001], [40, 16 * 1024 * 1024 + 1]]) {
    const file = zip64Fixture([publicItem]); file.eocd64.writeBigUInt64LE(BigInt(value), at);
    if (at === 32) file.eocd64.writeBigUInt64LE(BigInt(value), 24);
    await assert.rejects(ArchiveZip.open(file), /目录过大/);
    assert.ok(file.reads.every(([left, right]) => right - left <= 56));
  }
  const single = await ArchiveZip.open(zip64Fixture([{...publicItem, claimSize: 64 * 1024 * 1024 + 1}]));
  await assert.rejects(single.readPostFiles(), /64 MB/);
  const many = await ArchiveZip.open(zip64Fixture([1, 2, 3].map(index => ({...publicItem, name: `data/tweets-part${index}.js`, claimSize: 50 * 1024 * 1024}))));
  await assert.rejects(many.readPostFiles(), /128 MB/);
  const media = await ArchiveZip.open(zip64Fixture([publicItem, {name: 'data/tweets_media/huge.jpg', content: 'small', claimSize: 512 * 1024 * 1024 + 1}]));
  await assert.rejects(media.hashMedia(['huge.jpg'], {createHasher() {}}), /512 MB/);
});

test('ZIP64 multipart disk fields are rejected in locator, end record and central extras', async () => {
  for (const mutate of [
    file => file.locator.writeUInt32LE(1, 4),
    file => file.locator.writeUInt32LE(2, 16),
    file => file.eocd64.writeUInt32LE(1, 16),
    file => file.eocd64.writeUInt32LE(1, 20),
    file => file.eocd64.writeBigUInt64LE(0n, 24),
  ]) {
    const file = zip64Fixture([publicItem]); mutate(file);
    await assert.rejects(ArchiveZip.open(file), /分卷/);
  }
  await assert.rejects(ArchiveZip.open(zip64Fixture([{...publicItem, zip64Fields: ['startDisk'], startDisk: 1}])), /分卷/);
});

test('ZIP comment and footer signatures inside comment are handled', async () => {
  const file = zipFixture([publicItem], {comment: 'comment PK\u0005\u0006 extra'});
  assert.equal((await (await ArchiveZip.open(file)).readPostFiles())[0].text, publicItem.content);
});

test('classic central file comment containing a ZIP64 locator signature is not a locator', async () => {
  const centralComment = Buffer.alloc(20); centralComment.writeUInt32LE(0x07064b50);
  const file = zipFixture([{...publicItem, centralComment}]);
  assert.equal((await (await ArchiveZip.open(file)).readPostFiles())[0].text, publicItem.content);
});

test('expanded text caps refuse entire analysis instead of truncating posts', async () => {
  const reader = await ArchiveZip.open(zipFixture([{...publicItem, claimSize: 64 * 1024 * 1024 + 1}]));
  await assert.rejects(reader.readPostFiles(), /64 MB/);
  const many = await ArchiveZip.open(zipFixture([1, 2, 3].map(index => ({...publicItem, name: `data/tweets-part${index}.js`, claimSize: 50 * 1024 * 1024}))));
  await assert.rejects(many.readPostFiles(), /128 MB/);
});

test('missing media and ambiguous media are surfaced', async () => {
  const file = zipFixture([publicItem, {name: 'one/data/tweets_media/shared.jpg', content: 'one'}, {name: 'two/data/tweets_media/shared.jpg', content: 'two'}]);
  const reader = await ArchiveZip.open(file);
  await assert.rejects(reader.hashMedia(['shared.jpg'], {createHasher() {}}), /同名/);
});
