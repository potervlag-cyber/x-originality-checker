'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {deflateRawSync} = require('node:zlib');
const {createHash} = require('node:crypto');
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
    const directory = Buffer.alloc(46 + name.length);
    directory.writeUInt32LE(0x02014b50); directory.writeUInt16LE(20, 4); directory.writeUInt16LE(20, 6);
    directory.writeUInt16LE(flags, 8); directory.writeUInt16LE(method, 10); directory.writeUInt32LE(digest, 16);
    directory.writeUInt32LE(compressed.length, 20); directory.writeUInt32LE(size, 24); directory.writeUInt16LE(name.length, 28);
    directory.writeUInt32LE(offset, 42); name.copy(directory, 46);
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

test('ZIP64 and multipart disks are rejected explicitly', async () => {
  const file = zipFixture([publicItem]);
  const footer = file.sections.at(-1).bytes;
  footer.writeUInt16LE(0xffff, 10); footer.writeUInt16LE(0xffff, 8);
  await assert.rejects(ArchiveZip.open(file), /ZIP64/);
  const multipart = zipFixture([publicItem]);
  multipart.sections.at(-1).bytes.writeUInt16LE(1, 4);
  await assert.rejects(ArchiveZip.open(multipart), /分卷/);
});

test('ZIP comment and footer signatures inside comment are handled', async () => {
  const file = zipFixture([publicItem], {comment: 'comment PK\u0005\u0006 extra'});
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
