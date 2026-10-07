'use strict';

// A ZIP index over a Blob. Only selected post entries are decompressed;
// unrelated archive contents (including direct messages) are never parsed.
(() => {
  const MAX_UPLOAD_BYTES = 300 * 1024 * 1024;
  const MAX_DIRECTORY_BYTES = 16 * 1024 * 1024;
  const MAX_ENTRIES = 200000;
  const MAX_TEXT_ENTRY_BYTES = 64 * 1024 * 1024;
  const MAX_TEXT_TOTAL_BYTES = 128 * 1024 * 1024;
  const MAX_MEDIA_TOTAL_BYTES = 512 * 1024 * 1024;
  const PUBLIC_POST = /(?:^|\/)data\/(?:tweets?|posts?)(?:[-_]part\d+)?\.(?:js|json)$/i;
  const PUBLIC_NOTE = /(?:^|\/)data\/note-tweet(?:s)?(?:[-_]part\d+)?\.(?:js|json)$/i;
  const PUBLIC_MEDIA = /(?:^|\/)data\/(?:tweets?|posts?)_media\/[^/]+$/i;
  const CRC_TABLE = Uint32Array.from({length: 256}, (_, n) => {
    for (let k = 0; k < 8; k++) n = (n & 1) ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
    return n >>> 0;
  });
  const CP437 = 'ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ';
  const err = message => new Error(message);
  const basename = name => name.slice(name.lastIndexOf('/') + 1);
  const partOrder = entry => Number(entry.name.match(/[-_]part(\d+)\.(?:js|json)$/i)?.[1] || 0);
  const byPart = (left, right) => partOrder(left) - partOrder(right) || left.name.localeCompare(right.name, 'en', {numeric: true});
  const view = bytes => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const same = (left, right) => left.length === right.length && left.every((byte, i) => byte === right[i]);

  function crcUpdate(crc, bytes) {
    for (const byte of bytes) crc = CRC_TABLE[(crc ^ byte) & 255] ^ (crc >>> 8);
    return crc >>> 0;
  }

  function decodeName(bytes, flags) {
    try {
      if (flags & 0x800) return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
      return Array.from(bytes, byte => byte < 128 ? String.fromCharCode(byte) : CP437[byte - 128]).join('');
    } catch { throw err('ZIP 文件名编码损坏，无法读取归档。'); }
  }

  function zip64Extra(bytes) {
    const data = view(bytes);
    let result = null;
    for (let at = 0; at < bytes.length;) {
      if (at + 4 > bytes.length) throw err('ZIP 扩展字段损坏。');
      const tag = data.getUint16(at, true), length = data.getUint16(at + 2, true);
      at += 4;
      if (at + length > bytes.length) throw err('ZIP 扩展字段损坏。');
      if (tag === 1) {
        if (result) throw err('ZIP64 扩展字段重复。');
        result = bytes.subarray(at, at + length);
      }
      at += length;
    }
    return result;
  }

  function uint64(data, at, label) {
    if (at + 8 > data.byteLength) throw err(`ZIP64 ${label}不完整。`);
    const value = data.getBigUint64(at, true);
    if (value > BigInt(Number.MAX_SAFE_INTEGER)) throw err(`ZIP64 ${label}超过安全整数范围。`);
    return Number(value);
  }

  function inRange(start, length, end) {
    return start >= 0 && start <= end && length >= 0 && length <= end - start;
  }

  function directoryValues(bytes, values) {
    const extra = zip64Extra(bytes);
    if (values.size !== 0xffffffff && values.compressedSize !== 0xffffffff &&
        values.localAt !== 0xffffffff && values.startDisk !== 0xffff) return values;
    if (!extra) throw err('ZIP64 目录缺少大小或偏移扩展字段。');
    const data = view(extra);
    let at = 0;
    // The extra contains only the fields whose classic values are sentinels,
    // in this order. An offset-only extra starts with the offset, not a size.
    for (const key of ['size', 'compressedSize', 'localAt']) {
      if (values[key] === 0xffffffff) {
        values[key] = uint64(data, at, '目录扩展字段');
        at += 8;
      }
    }
    if (values.startDisk === 0xffff) {
      if (at + 4 > data.byteLength) throw err('ZIP64 分卷扩展字段不完整。');
      values.startDisk = data.getUint32(at, true);
    }
    return values;
  }

  function localValues(bytes, size, compressedSize) {
    const extra = zip64Extra(bytes);
    if (size !== 0xffffffff && compressedSize !== 0xffffffff) return {size, compressedSize};
    if (!extra || extra.length < 16) throw err('ZIP64 文件头缺少大小扩展字段。');
    // Unlike the central directory, a ZIP64 local header carries both sizes.
    const data = view(extra), expanded = uint64(data, 0, '文件头大小'), compressed = uint64(data, 8, '文件头大小');
    return {size: size === 0xffffffff ? expanded : size,
      compressedSize: compressedSize === 0xffffffff ? compressed : compressedSize};
  }

  async function open(file, options = {}) {
    if (!file || typeof file.slice !== 'function' || !Number.isSafeInteger(file.size) || file.size < 22) throw err('请选择从 X 下载的完整 ZIP 归档。');
    if (file.size > MAX_UPLOAD_BYTES) throw err('ZIP 最大支持 300 MB，请选择不超过 300 MB 的文件。');
    const notify = message => { if (typeof options.onProgress === 'function') options.onProgress(message); };
    notify('正在读取 ZIP 目录；不解析私信和账号资料…');
    // X usually has no ZIP comment: read only the 22-byte footer first.
    let tailStart = file.size - 22;
    let tail = new Uint8Array(await file.slice(tailStart).arrayBuffer());
    if (tail.length !== 22 || view(tail).getUint32(0, true) !== 0x06054b50 || view(tail).getUint16(20, true) !== 0) {
      tailStart = Math.max(0, file.size - 65557);
      tail = new Uint8Array(await file.slice(tailStart).arrayBuffer());
    }
    const tailView = view(tail);
    let endAt = -1;
    for (let at = tail.length - 22; at >= 0; at--) {
      if (tailView.getUint32(at, true) === 0x06054b50 && at + 22 + tailView.getUint16(at + 20, true) === tail.length) { endAt = at; break; }
    }
    if (endAt < 0) throw err('找不到 ZIP 完整目录。文件可能损坏、尚未下载完成或不是 ZIP。');
    const endOffset = tailStart + endAt;
    const disk = tailView.getUint16(endAt + 4, true), directoryDisk = tailView.getUint16(endAt + 6, true);
    let diskEntries = tailView.getUint16(endAt + 8, true), entryCount = tailView.getUint16(endAt + 10, true);
    let directoryBytes = tailView.getUint32(endAt + 12, true), directoryAt = tailView.getUint32(endAt + 16, true);
    let directoryEnd = endOffset;
    // Read the locator by absolute position, including on the 22-byte fast
    // path. Forced ZIP64 writers may keep every classic EOCD value in range.
    const locatorAt = endOffset - 20;
    const locatorBytes = locatorAt >= 0 ? new Uint8Array(await file.slice(locatorAt, endOffset).arrayBuffer()) : new Uint8Array();
    // A signature inside the final directory entry's comment is directory
    // data, not a locator. A real ZIP64 directory ends before its EOCD64.
    const classicDirectoryEndsAtEOCD = inRange(directoryAt, directoryBytes, endOffset) && directoryBytes === endOffset - directoryAt;
    if (!classicDirectoryEndsAtEOCD && locatorBytes.length === 20 && view(locatorBytes).getUint32(0, true) === 0x07064b50) {
      const locator = view(locatorBytes);
      if (locator.getUint32(4, true) !== 0 || locator.getUint32(16, true) !== 1) throw err('暂不支持分卷 ZIP64。请选择完整的单个 ZIP 文件。');
      const zip64At = uint64(locator, 8, '目录结束记录偏移');
      if (!inRange(zip64At, 56, locatorAt)) throw err('ZIP64 目录结束记录偏移超出范围或记录不完整。');
      const recordBytes = new Uint8Array(await file.slice(zip64At, zip64At + 56).arrayBuffer());
      const record = view(recordBytes);
      if (recordBytes.length !== 56 || record.getUint32(0, true) !== 0x06064b50) throw err('ZIP64 目录结束记录损坏。');
      const recordSize = uint64(record, 4, '目录结束记录长度');
      if (recordSize < 44 || recordSize !== locatorAt - zip64At - 12) throw err('ZIP64 目录结束记录长度或偏移不一致。');
      const zip64Disk = record.getUint32(16, true), zip64DirectoryDisk = record.getUint32(20, true);
      const zip64DiskEntries = uint64(record, 24, '分卷文件数'), zip64Entries = uint64(record, 32, '文件数');
      const zip64DirectoryBytes = uint64(record, 40, '目录长度'), zip64DirectoryAt = uint64(record, 48, '目录偏移');
      if (zip64Disk || zip64DirectoryDisk || zip64DiskEntries !== zip64Entries ||
          (disk !== 0 && disk !== 0xffff) || (directoryDisk !== 0 && directoryDisk !== 0xffff)) throw err('暂不支持分卷 ZIP64。请选择完整的单个 ZIP 文件。');
      if ((diskEntries !== 0xffff && diskEntries !== zip64DiskEntries) ||
          (entryCount !== 0xffff && entryCount !== zip64Entries) ||
          (directoryBytes !== 0xffffffff && directoryBytes !== zip64DirectoryBytes) ||
          (directoryAt !== 0xffffffff && directoryAt !== zip64DirectoryAt)) throw err('ZIP64 目录与普通目录结束记录不一致。');
      diskEntries = zip64DiskEntries;
      entryCount = zip64Entries;
      directoryBytes = zip64DirectoryBytes;
      directoryAt = zip64DirectoryAt;
      directoryEnd = zip64At;
    } else {
      if (disk || directoryDisk || diskEntries !== entryCount) throw err('暂不支持分卷 ZIP。请选择完整的单个 ZIP 文件。');
      if (directoryBytes === 0xffffffff || directoryAt === 0xffffffff) throw err('ZIP64 缺少目录定位记录。');
      // A classic ZIP may have exactly 65,535 entries without a ZIP64 footer.
      // Its count and complete directory are validated below.
    }
    if (entryCount > MAX_ENTRIES || directoryBytes > MAX_DIRECTORY_BYTES) throw err('ZIP 目录过大，超过浏览器安全处理范围。');
    if (!inRange(directoryAt, directoryBytes, directoryEnd) || directoryBytes !== directoryEnd - directoryAt) throw err('ZIP 目录偏移不一致，文件可能损坏。');
    const directory = new Uint8Array(await file.slice(directoryAt, directoryAt + directoryBytes).arrayBuffer());
    if (directory.length !== directoryBytes) throw err('ZIP 目录不完整。');
    const data = view(directory), entries = [], seen = new Set();
    let at = 0;
    for (let index = 0; index < entryCount; index++) {
      if (at + 46 > directory.length || data.getUint32(at, true) !== 0x02014b50) throw err('ZIP 目录记录损坏。');
      const flags = data.getUint16(at + 8, true), method = data.getUint16(at + 10, true);
      const crc = data.getUint32(at + 16, true);
      const nameBytes = data.getUint16(at + 28, true), extraBytes = data.getUint16(at + 30, true), commentBytes = data.getUint16(at + 32, true);
      const next = at + 46 + nameBytes + extraBytes + commentBytes;
      if (next > directory.length || nameBytes === 0) throw err('ZIP 目录记录不完整。');
      const {size, compressedSize, localAt, startDisk} = directoryValues(
        directory.subarray(at + 46 + nameBytes, at + 46 + nameBytes + extraBytes),
        {size: data.getUint32(at + 24, true), compressedSize: data.getUint32(at + 20, true),
          localAt: data.getUint32(at + 42, true), startDisk: data.getUint16(at + 34, true)});
      const rawName = directory.slice(at + 46, at + 46 + nameBytes);
      const name = decodeName(rawName, flags);
      if (name.includes('\\') || name.startsWith('/') || name.includes('\0') || name.split('/').some(part => part === '..') || /^[a-z]:/i.test(name)) throw err('ZIP 包含无效路径，已停止处理。');
      if (seen.has(name)) throw err('ZIP 含有重名文件，无法可靠判断归档内容。');
      seen.add(name);
      if (startDisk) throw err('暂不支持分卷 ZIP。');
      if (!inRange(localAt, 30, directoryAt) || compressedSize > directoryAt - localAt - 30) throw err('ZIP 文件数据偏移超出范围。');
      const unixMode = data.getUint32(at + 38, true) >>> 16;
      entries.push(Object.freeze({name, rawName, flags, method, crc, compressedSize, size, localAt, symlink: (unixMode & 0xf000) === 0xa000}));
      at = next;
    }
    if (at !== directory.length) throw err('ZIP 目录含有不支持的额外记录，无法确认全部帖子。');
    const entrySet = new Set(entries);
    const postEntries = entries.filter(entry => PUBLIC_POST.test(entry.name));
    const noteEntries = entries.filter(entry => PUBLIC_NOTE.test(entry.name));
    if (!postEntries.length) throw err('ZIP 中未找到 X 的 data/tweets.js 或 data/posts.js 发帖归档。请上传从 X 下载的完整归档 ZIP。');
    postEntries.sort(byPart);
    noteEntries.sort(byPart);

    async function readChunks(entry, onChunk) {
      if (!entrySet.has(entry)) throw err('读取目标不属于当前 ZIP。');
      if (entry.flags & 0x2041) throw err('发帖或引用媒体使用了加密 ZIP；请提供未加密的 X 归档。');
      if (entry.symlink) throw err('发帖或媒体文件不能是符号链接。');
      if (entry.method !== 0 && entry.method !== 8) throw err('归档使用了不支持的压缩方式；仅支持 ZIP/ZIP64（stored/deflate）。');
      const header = new Uint8Array(await file.slice(entry.localAt, entry.localAt + 30).arrayBuffer());
      const local = view(header);
      if (header.length !== 30 || local.getUint32(0, true) !== 0x04034b50) throw err('ZIP 文件头损坏。');
      const nameLength = local.getUint16(26, true), extraLength = local.getUint16(28, true);
      if (local.getUint16(6, true) !== entry.flags || local.getUint16(8, true) !== entry.method) throw err('ZIP 文件头与目录记录不一致。');
      const fieldsAt = entry.localAt + 30, fieldsLength = nameLength + extraLength;
      if (!inRange(fieldsAt, fieldsLength, directoryAt) || entry.compressedSize > directoryAt - fieldsAt - fieldsLength) throw err('ZIP 文件正文超出数据范围。');
      const payloadAt = fieldsAt + fieldsLength;
      const fields = new Uint8Array(await file.slice(fieldsAt, payloadAt).arrayBuffer());
      if (fields.length !== fieldsLength) throw err('ZIP 文件头扩展字段不完整。');
      const rawName = fields.subarray(0, nameLength);
      if (!same(rawName, entry.rawName)) throw err('ZIP 文件名与目录记录不一致。');
      const sizes = localValues(fields.subarray(nameLength), local.getUint32(22, true), local.getUint32(18, true));
      if (!(entry.flags & 8) && (local.getUint32(14, true) !== entry.crc || sizes.compressedSize !== entry.compressedSize || sizes.size !== entry.size)) throw err('ZIP 长度或校验值与目录记录不一致。');
      let stream = file.slice(payloadAt, payloadAt + entry.compressedSize).stream();
      if (entry.method === 8) {
        try {
          if (typeof options.inflateRaw === 'function') stream = await options.inflateRaw(stream, entry);
          else stream = stream.pipeThrough(new DecompressionStream('deflate-raw'));
        } catch { throw err('此浏览器无法解压该 ZIP，请使用最新版 Chrome 或 Edge 后重试。'); }
      }
      const reader = stream.getReader();
      let expanded = 0, crc = 0xffffffff;
      try {
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
          expanded += chunk.length;
          if (expanded > entry.size) throw err('ZIP 解压长度超过声明值，已停止处理。');
          crc = crcUpdate(crc, chunk);
          await onChunk(chunk);
        }
        if (expanded !== entry.size || ((crc ^ 0xffffffff) >>> 0) !== entry.crc) throw err('ZIP 正文长度或 CRC 校验不符，文件可能损坏。');
        return expanded;
      } catch (error) {
        try { await reader.cancel(); } catch { /* The decompressor may already have failed. */ }
        if (error?.message?.startsWith('ZIP ')) throw error;
        throw err('ZIP 解压或读取失败，文件可能损坏。');
      } finally { reader.releaseLock(); }
    }

    async function readText(entry) {
      if (entry.size > MAX_TEXT_ENTRY_BYTES) throw err('单个发帖归档解压后超过 64 MB，超过浏览器处理范围；未截取部分帖子。');
      const decoder = new TextDecoder('utf-8', {fatal: true}), pieces = [];
      try {
        await readChunks(entry, chunk => { pieces.push(decoder.decode(chunk, {stream: true})); });
        pieces.push(decoder.decode());
      } catch (error) {
        if (error instanceof TypeError) throw err('发帖归档不是有效 UTF-8 文本。');
        throw error;
      }
      return pieces.join('');
    }

    async function readPostFiles(readOptions = {}) {
      const selected = [...postEntries, ...(readOptions.includeNotes ? noteEntries : [])];
      if (selected.reduce((sum, entry) => sum + entry.size, 0) > MAX_TEXT_TOTAL_BYTES) throw err('发帖归档解压后总量超过 128 MB，超过浏览器处理范围；未截取部分帖子。');
      const files = [];
      for (const [index, entry] of selected.entries()) {
        notify(`正在解压发帖归档 ${index + 1} / ${selected.length}…`);
        files.push({name: entry.name, text: await readText(entry)});
      }
      return files;
    }

    async function hashMedia(requestedNames, mediaOptions = {}) {
      if (typeof mediaOptions.createHasher !== 'function') throw err('媒体哈希引擎尚未准备好。');
      const requested = [...new Set(requestedNames)].filter(name => typeof name === 'string' && name);
      const requestedSet = new Set(requested);
      const mediaByName = new Map();
      for (const entry of entries.filter(item => PUBLIC_MEDIA.test(item.name))) {
        const shortName = basename(entry.name);
        if (!requestedSet.has(shortName) && !requestedSet.has(entry.name)) continue;
        if (mediaByName.has(shortName)) throw err('引用媒体含有同名文件，无法可靠核对媒体。');
        mediaByName.set(shortName, entry);
      }
      const pairs = requested.map(name => [name, mediaByName.get(basename(name))]).filter(([, entry]) => entry);
      if ([...new Set(pairs.map(([, entry]) => entry))].reduce((sum, entry) => sum + entry.size, 0) > MAX_MEDIA_TOTAL_BYTES) throw err('引用媒体解压后超过 512 MB，超过浏览器处理范围；未跳过部分媒体。');
      const hashes = [], missing = requested.filter(name => !mediaByName.has(basename(name))), cached = new Map();
      let hashedBytes = 0;
      for (const [index, [name, entry]] of pairs.entries()) {
        const progress = mediaOptions.onProgress || notify;
        progress(`正在核对发帖媒体 ${index + 1} / ${pairs.length}…`);
        let digest = cached.get(entry);
        if (!digest) {
          const hasher = await mediaOptions.createHasher();
          try {
            hashedBytes += await readChunks(entry, chunk => hasher.update(chunk));
            digest = await hasher.digest();
          } finally { if (typeof hasher.dispose === 'function') hasher.dispose(); }
          if (!/^[a-f0-9]{64}$/i.test(String(digest))) throw err('媒体哈希结果无效。');
          cached.set(entry, digest);
        }
        hashes.push({name, hash: digest.toLowerCase(), hash_origin: 'local_file'});
      }
      return {hashes, missing, hashed_bytes: hashedBytes};
    }

    return Object.freeze({entries, postEntries, noteEntries, readText, readChunks, readPostFiles, hashMedia,
      metadata: Object.freeze({input_bytes: file.size, zip_entries: entries.length, post_files: postEntries.length,
        public_post_files: postEntries.length, note_files: noteEntries.length,
        non_post_entries: entries.length - postEntries.length - noteEntries.length,
        ignored_entries: entries.length - postEntries.length - noteEntries.length})});
  }

  globalThis.ArchiveZip = Object.freeze({MAX_UPLOAD_BYTES, open});
})();
